#!/usr/bin/env python3
"""
Health check script for Docker and monitoring.

Checks that the bot process (main.py) is running.
Exit code 0 = healthy, 1 = unhealthy.
"""
import os
import sys


def check_bot_running() -> bool:
    """Check if python main.py process is alive by scanning /proc."""
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                cmdline_path = f"/proc/{pid}/cmdline"
                with open(cmdline_path, "rb") as f:
                    cmdline = f.read().decode("utf-8", errors="replace")
                # cmdline is null-separated
                if "main.py" in cmdline:
                    return True
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
    except Exception:
        pass
    return False


if __name__ == "__main__":
    if check_bot_running():
        sys.exit(0)
    else:
        sys.exit(1)
