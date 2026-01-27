#!/usr/bin/env python3
"""
Post-Edit Lint Hook for Claude Code
Runs appropriate linters/formatters after file edits based on file extension.
"""

import json
import os
import subprocess
import sys
from pathlib import Path


def run_command(cmd: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    """Run a command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return 1, "", "Command timed out"
    except FileNotFoundError:
        return 1, "", f"Command not found: {cmd[0]}"
    except Exception as e:
        return 1, "", str(e)


def lint_python_file(file_path: str) -> list[str]:
    """Run Python linters on a file. Returns list of issues."""
    issues = []

    # Run ruff check --fix
    returncode, stdout, stderr = run_command(["ruff", "check", "--fix", file_path])
    if returncode != 0 and stderr:
        issues.append(f"ruff: {stderr.strip()}")

    # Run pyright (don't fail on type errors, just report)
    returncode, stdout, stderr = run_command(["pyright", file_path])
    if returncode != 0:
        # Pyright outputs to stdout
        output = stdout.strip() or stderr.strip()
        if output and "error" in output.lower():
            # Only report actual errors, not the summary line
            error_lines = [l for l in output.split('\n') if 'error:' in l.lower()]
            if error_lines:
                issues.append(f"pyright: {len(error_lines)} type error(s)")

    return issues


def lint_frontend_file(file_path: str) -> list[str]:
    """Run Frontend linters on a file. Returns list of issues."""
    issues = []

    # Run prettier --write
    returncode, stdout, stderr = run_command(["prettier", "--write", file_path])
    if returncode != 0 and stderr:
        issues.append(f"prettier: {stderr.strip()}")

    # Run eslint --fix
    returncode, stdout, stderr = run_command(["eslint", "--fix", file_path])
    if returncode != 0:
        output = stdout.strip() or stderr.strip()
        if output:
            # Count actual problems
            if "problem" in output.lower():
                issues.append("eslint: found issues")

    return issues


def main():
    """Main hook function."""
    # Read input from stdin
    try:
        raw_input = sys.stdin.read()
        input_data = json.loads(raw_input)
    except json.JSONDecodeError:
        sys.exit(0)  # Allow tool to proceed if we can't parse input

    # Extract tool information
    tool_input = input_data.get("tool_input", {})
    file_path = tool_input.get("file_path", "")

    if not file_path or not os.path.exists(file_path):
        sys.exit(0)

    # Determine file type and run appropriate linters
    path = Path(file_path)
    suffix = path.suffix.lower()
    issues = []

    if suffix == ".py":
        issues = lint_python_file(file_path)
    elif suffix in (".ts", ".tsx", ".js", ".jsx"):
        issues = lint_frontend_file(file_path)

    # Report issues to stderr (informational, don't block)
    if issues:
        print(f"Lint results for {path.name}:", file=sys.stderr)
        for issue in issues:
            print(f"  - {issue}", file=sys.stderr)

    # Always exit 0 - PostToolUse hooks shouldn't block
    sys.exit(0)


if __name__ == "__main__":
    main()
