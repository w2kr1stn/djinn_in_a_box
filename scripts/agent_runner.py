#!/usr/bin/env python3
"""
AI Dev Base - Agent Runner

Runs CLI coding agents (Claude, Gemini, Codex, OpenCode) in headless mode
inside the ai-dev-base Docker container.

Usage as CLI:
    python scripts/agent_runner.py build-cmd claude --write --json
    python scripts/agent_runner.py run claude "Explain the architecture"
    python scripts/agent_runner.py run gemini "Refactor main.py" --write --json
    python scripts/agent_runner.py list

Usage as module:
    from agent_runner import AgentRunner

    runner = AgentRunner(workspace=".")
    result = runner.run("claude", "Explain the architecture")
    print(result.stdout)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).parent.parent / "config" / "agents.json"


@dataclass
class AgentConfig:
    """Configuration for a single CLI agent."""

    name: str
    binary: str
    description: str
    headless_flags: list[str]
    read_only_flags: list[str]
    write_flags: list[str]
    json_flags: list[str]
    model_flag: str
    prompt_via: str
    prompt_env_var: str
    prompt_template: str


def load_agents_config(config_path: Path = CONFIG_PATH) -> dict[str, AgentConfig]:
    """Load agent configurations from agents.json."""
    if not config_path.exists():
        raise FileNotFoundError(f"Agent config not found: {config_path}")

    with open(config_path) as f:
        data = json.load(f)

    agents = {}
    for name, cfg in data["agents"].items():
        agents[name] = AgentConfig(
            name=name,
            binary=cfg["binary"],
            description=cfg.get("description", ""),
            headless_flags=cfg.get("headless_flags", []),
            read_only_flags=cfg.get("read_only_flags", []),
            write_flags=cfg.get("write_flags", []),
            json_flags=cfg.get("json_flags", []),
            model_flag=cfg.get("model_flag", "--model"),
            prompt_via=cfg.get("prompt_via", "env"),
            prompt_env_var=cfg.get("prompt_env_var", "AGENT_PROMPT"),
            prompt_template=cfg.get("prompt_template", '"$AGENT_PROMPT"'),
        )
    return agents


# ---------------------------------------------------------------------------
# Command building
# ---------------------------------------------------------------------------


def build_shell_command(
    agent: str,
    *,
    write: bool = False,
    json_output: bool = False,
    model: Optional[str] = None,
    config_path: Path = CONFIG_PATH,
) -> str:
    """
    Build the shell command string for running an agent in headless mode.

    The prompt is referenced via environment variable (not embedded in the command)
    to avoid shell escaping issues.

    Args:
        agent: Agent name (claude, gemini, codex, opencode)
        write: Allow file modifications
        json_output: Request JSON output format
        model: Specific model to use (e.g. 'sonnet', 'gemini-2.5-flash')
        config_path: Path to agents.json

    Returns:
        Shell command string suitable for 'zsh -c "..."'
    """
    agents = load_agents_config(config_path)

    if agent not in agents:
        available = ", ".join(agents.keys())
        raise ValueError(f"Unknown agent '{agent}'. Available: {available}")

    cfg = agents[agent]
    parts: list[str] = [cfg.binary]
    parts.extend(cfg.headless_flags)

    if model:
        parts.extend([cfg.model_flag, model])

    if write:
        parts.extend(cfg.write_flags)
    else:
        parts.extend(cfg.read_only_flags)

    if json_output:
        parts.extend(cfg.json_flags)

    # Append prompt reference (env var, expanded at runtime in container)
    parts.append(cfg.prompt_template)

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Docker execution
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    """Result of an agent run."""

    stdout: str
    stderr: str
    returncode: int
    agent: str
    command: str


@dataclass
class AgentRunner:
    """
    Runs CLI agents in headless mode inside the ai-dev-base Docker container.

    Example:
        runner = AgentRunner(workspace="/path/to/project")
        result = runner.run("claude", "Explain this codebase")
        print(result.stdout)
    """

    workspace: str = "."
    docker_enabled: bool = False
    firewall_enabled: bool = False
    config_path: Path = field(default_factory=lambda: CONFIG_PATH)

    def __post_init__(self) -> None:
        self.workspace = str(Path(self.workspace).resolve())
        self._project_dir = str(Path(__file__).parent.parent.resolve())

    def run(
        self,
        agent: str,
        prompt: str,
        *,
        write: bool = False,
        json_output: bool = False,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> RunResult:
        """
        Run an agent in headless mode and return the result.

        Args:
            agent: Agent name (claude, gemini, codex, opencode)
            prompt: The prompt to send to the agent
            write: Allow the agent to modify files (default: read-only)
            json_output: Request JSON output format
            model: Specific model to use (e.g. 'sonnet', 'gemini-2.5-flash')
            timeout: Timeout in seconds (None = no timeout)

        Returns:
            RunResult with stdout, stderr, returncode
        """
        agent_cmd = build_shell_command(
            agent,
            write=write,
            json_output=json_output,
            model=model,
            config_path=self.config_path,
        )

        # Build docker compose command
        compose_files = ["-f", "docker-compose.yml"]
        if self.docker_enabled:
            compose_files.extend(["-f", "docker-compose.docker.yml"])

        docker_cmd = [
            "docker",
            "compose",
            *compose_files,
            "run",
            "--rm",
            "-T",
            "-e",
            "AGENT_PROMPT",
            "-v",
            f"{self.workspace}:/home/dev/workspace",
            "--workdir",
            "/home/dev/workspace",
            "dev",
            "-c",
            agent_cmd,
        ]

        # Set up environment
        env = os.environ.copy()
        env["AGENT_PROMPT"] = prompt
        if self.firewall_enabled:
            env["ENABLE_FIREWALL"] = "true"

        try:
            result = subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=self._project_dir,
                env=env,
            )
        except subprocess.TimeoutExpired as e:
            stdout = (
                e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
            )
            stderr = (
                e.stderr.decode()
                if isinstance(e.stderr, bytes)
                else (e.stderr or f"Timeout after {timeout}s")
            )
            return RunResult(
                stdout=stdout,
                stderr=stderr,
                returncode=124,
                agent=agent,
                command=agent_cmd,
            )

        return RunResult(
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
            agent=agent,
            command=agent_cmd,
        )

    def list_agents(self) -> dict[str, AgentConfig]:
        """List available agents and their configurations."""
        return load_agents_config(self.config_path)


# ---------------------------------------------------------------------------
# CLI interface
# ---------------------------------------------------------------------------


def cli_build_cmd(args: argparse.Namespace) -> None:
    """Handle 'build-cmd' subcommand."""
    try:
        cmd = build_shell_command(
            args.agent,
            write=args.write,
            json_output=args.json,
            model=args.model,
        )
        print(cmd)
    except (ValueError, FileNotFoundError) as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)


def cli_run(args: argparse.Namespace) -> None:
    """Handle 'run' subcommand."""
    workspace = args.workspace or os.getcwd()

    runner = AgentRunner(
        workspace=workspace,
        docker_enabled=args.docker,
        firewall_enabled=args.firewall,
    )

    # Status output to stderr (keep stdout clean for agent output)
    print(f"\nRunning {args.agent} (headless)...", file=sys.stderr)
    print(f"   Workspace: {runner.workspace}", file=sys.stderr)
    mode = "Read/Write" if args.write else "Read-only"
    print(f"   Mode:      {mode}", file=sys.stderr)
    if args.model:
        print(f"   Model:     {args.model}", file=sys.stderr)
    if args.json:
        print("   Output:    JSON", file=sys.stderr)
    print("", file=sys.stderr)

    result = runner.run(
        args.agent,
        args.prompt,
        write=args.write,
        json_output=args.json,
        model=args.model,
        timeout=args.timeout,
    )

    # Output agent response
    if result.stdout:
        print(result.stdout, end="")

    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")

    sys.exit(result.returncode)


def cli_list(args: argparse.Namespace) -> None:
    """Handle 'list' subcommand."""
    try:
        agents = load_agents_config()
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    fmt = args.format if hasattr(args, "format") else "table"

    if fmt == "json":
        data = {
            name: {
                "binary": cfg.binary,
                "description": cfg.description,
                "headless_flags": cfg.headless_flags,
            }
            for name, cfg in agents.items()
        }
        print(json.dumps(data, indent=2))
    else:
        print(f"{'Agent':<12} {'Binary':<12} {'Description'}")
        print("-" * 60)
        for name, cfg in agents.items():
            print(f"{name:<12} {cfg.binary:<12} {cfg.description}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="agent_runner",
        description="Run CLI coding agents in headless mode via Docker container",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- build-cmd ---
    build_parser = subparsers.add_parser(
        "build-cmd",
        help="Build shell command for an agent (used by dev.sh)",
    )
    build_parser.add_argument(
        "agent", choices=["claude", "gemini", "codex", "opencode"]
    )
    build_parser.add_argument("--write", action="store_true", help="Enable write mode")
    build_parser.add_argument(
        "--json", action="store_true", help="Enable JSON output format"
    )
    build_parser.add_argument(
        "--model", help="Model to use (e.g. sonnet, gemini-2.5-flash)"
    )
    build_parser.set_defaults(func=cli_build_cmd)

    # --- run ---
    run_parser = subparsers.add_parser(
        "run",
        help="Run an agent in headless mode (full Docker execution)",
    )
    run_parser.add_argument("agent", choices=["claude", "gemini", "codex", "opencode"])
    run_parser.add_argument("prompt", help="Prompt to send to the agent")
    run_parser.add_argument("--write", action="store_true", help="Allow file changes")
    run_parser.add_argument("--json", action="store_true", help="JSON output format")
    run_parser.add_argument(
        "--model", help="Model to use (e.g. sonnet, gemini-2.5-flash)"
    )
    run_parser.add_argument("--docker", action="store_true", help="Enable Docker proxy")
    run_parser.add_argument("--firewall", action="store_true", help="Enable firewall")
    run_parser.add_argument("--workspace", "-w", help="Workspace path (default: cwd)")
    run_parser.add_argument("--timeout", "-t", type=float, help="Timeout in seconds")
    run_parser.set_defaults(func=cli_run)

    # --- list ---
    list_parser = subparsers.add_parser("list", help="List available agents")
    list_parser.add_argument("--format", choices=["table", "json"], default="table")
    list_parser.set_defaults(func=cli_list)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
