"""DevOps tasks for Djinn in a Box.

Invoked via pyproject.toml scripts: uv run fmt, uv run test, uv run clean
"""

import subprocess
import sys


def _run(commands: list[list[str]]) -> None:
    for cmd in commands:
        try:
            subprocess.run(cmd, check=True)
        except FileNotFoundError:
            print(f"Command not found: {cmd[0]}", file=sys.stderr)
            sys.exit(127)
        except subprocess.CalledProcessError as e:
            print(f"Command failed: {' '.join(e.cmd)}", file=sys.stderr)
            sys.exit(e.returncode)


def format_code() -> None:
    _run(
        [
            ["ruff", "format", "."],
            ["ruff", "check", "--fix", "."],
        ]
    )


def test() -> None:
    _run(
        [
            ["uv", "run", "pytest", "-q"],
        ]
    )


def clean() -> None:
    _run(
        [
            ["find", ".", "-type", "d", "-name", "__pycache__", "-exec", "rm", "-rf", "{}", "+"],
            ["find", ".", "-type", "f", "-name", "*.pyc", "-delete"],
        ]
    )
