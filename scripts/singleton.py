"""Hold the Ledgerly data-volume lock before starting the application."""

from __future__ import annotations

import fcntl
import os
import sys
from pathlib import Path


def acquire_lock(path: Path):
    """Return an open, exclusively locked file or exit with a clear error."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError(f"another Ledgerly process owns {path}") from exc
    return handle


def main() -> int:
    path = Path(os.getenv("LEDGERLY_SINGLETON_PATH", "data/.ledgerly-singleton.lock"))
    try:
        lock_handle = acquire_lock(path)
    except RuntimeError as exc:
        print(f"Ledgerly startup refused: {exc}", file=sys.stderr)
        return 75

    # Keep the descriptor and its flock alive across exec into the real app.
    os.set_inheritable(lock_handle.fileno(), True)
    os.execvp("python", ["python", "main.py"])
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
