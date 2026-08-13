#!/usr/bin/env python3
"""
Health check script for Docker and monitoring.

Checks the application's local event loop and SQLite connection.
Exit code 0 = healthy, 1 = unhealthy.
"""
import os
import sys
import urllib.error
import urllib.request


def check_app_liveness() -> bool:
    """Return whether the local non-disclosing liveness endpoint responds."""
    try:
        port = int(os.getenv("PORT", "8080"))
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/livez", timeout=2
        ) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


check_bot_running = check_app_liveness


if __name__ == "__main__":
    if check_app_liveness():
        sys.exit(0)
    else:
        sys.exit(1)
