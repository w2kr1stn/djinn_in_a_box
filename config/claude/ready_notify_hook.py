#!/usr/bin/env python3
"""
Ready Notification Hook for Claude Code
Notifies the user when Claude Code is done and waiting for input.
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
        with open("/dev/tty", "w") as tty:
            tty.write("\a")
            tty.flush()
        return True
    except OSError:
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
    try:
        raw_input = sys.stdin.read()
        input_data = json.loads(raw_input)
    except json.JSONDecodeError:
        sys.exit(0)

    stop_reason = input_data.get("stop_reason", "done")

    send_notification(
        "Claude Code",
        f"Ready for input ({stop_reason})",
    )

    sys.exit(0)


if __name__ == "__main__":
    main()
