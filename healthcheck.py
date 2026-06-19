#!/usr/bin/env python3
"""
Health check script for Docker and monitoring.

Checks that the bot process (main.py) is running.
Exit code 0 = healthy, 1 = unhealthy.
"""
import subprocess
import sys


def check_bot_running() -> bool:
    """Check if python main.py process is alive."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "python main.py"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


if __name__ == "__main__":
    if check_bot_running():
        sys.exit(0)
    else:
        sys.exit(1)
