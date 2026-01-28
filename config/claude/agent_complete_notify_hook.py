#!/usr/bin/env python3
"""
Agent Complete Notification Hook for Claude Code
Notifies the user when a subagent completes its work.
"""

import json
import subprocess
import sys


def send_notification(title: str, message: str) -> bool:
    """Try multiple notification methods."""

    # Method 1: Desktop notification via notify-send (Linux)
    try:
        result = subprocess.run(
            ["notify-send", "--urgency=normal", "--expire-time=5000", title, message],
            capture_output=True,
            timeout=5,
        )
        if result.returncode == 0:
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Method 2: Terminal bell via tput
    try:
        subprocess.run(["tput", "bel"], timeout=2)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Method 3: Write bell character directly to terminal
    try:
        # Try to write to the controlling terminal
        with open("/dev/tty", "w") as tty:
            tty.write("\a")
            tty.flush()
        return True
    except (OSError, IOError):
        pass

    # Method 4: Print bell to stderr (might work in some terminals)
    try:
        print("\a", file=sys.stderr, end="", flush=True)
        return True
    except Exception:
        pass

    return False


def main():
    """Main hook function."""
    # Read input from stdin
    try:
        raw_input = sys.stdin.read()
        input_data = json.loads(raw_input)
    except json.JSONDecodeError:
        sys.exit(0)

    # Extract agent information
    agent_type = input_data.get("agent_type", "unknown")
    # session_id = input_data.get("session_id", "")

    # Send notification
    send_notification(
        "Claude Code Agent Complete",
        f"The {agent_type} agent has finished its task.",
    )

    sys.exit(0)


if __name__ == "__main__":
    main()
