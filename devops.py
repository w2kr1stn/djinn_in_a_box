"""DevOps tasks for toolkit-infra.

Usage: uv run devops.py <task>
Tasks: fmt, test, clean
"""

import subprocess
import sys


def _run(commands: list[list[str]]) -> None:
    """Execute a sequence of shell commands, exiting on first failure."""
    for cmd in commands:
        try:
            subprocess.run(cmd, check=True)  # nosec: B603, B607
        except subprocess.CalledProcessError as e:
            print(f"Command failed: {' '.join(e.cmd)}", file=sys.stderr)
            sys.exit(e.returncode)


def format_code() -> None:
    """Format the codebase with Ruff."""
    _run(
        [
            ["ruff", "format", "."],
            ["ruff", "check", "--fix", "."],
        ]
    )


def test() -> None:
    """Run tests with PyTest."""
    _run(
        [
            ["uv", "run", "pytest", "-q"],
        ]
    )


def clean() -> None:
    """Clean up the project."""
    _run(
        [
            ["find", ".", "-type", "d", "-name", "__pycache__", "-exec", "rm", "-rf", "{}", "+"],
            ["find", ".", "-type", "f", "-name", "*.pyc", "-delete"],
        ]
    )
