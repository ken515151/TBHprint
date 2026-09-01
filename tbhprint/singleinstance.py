"""Single-instance locks for the agent and the tray.

Windows: a named mutex (`Local\\TBHprint-agent` / `Local\\TBHprint-tray`) -
`CreateMutex` plus `ERROR_ALREADY_EXISTS` tells us someone else already
holds it. Linux/macOS: `flock` on a file under the state dir. Either way
the OS releases the lock automatically if the process dies, so a crash
never wedges the next start.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys

log = logging.getLogger("tbhprint.singleinstance")

AGENT_MUTEX_NAME = "Local\\TBHprint-agent"
TRAY_MUTEX_NAME = "Local\\TBHprint-tray"


class AlreadyRunning(RuntimeError):
    """Another instance already holds this lock."""


class SingleInstanceLock:
    """`name` is the Windows mutex name; `lock_path` is the Linux/macOS lock
    file path (required on those platforms). Use as a context manager or
    call acquire()/release() directly."""

    def __init__(self, name: str, lock_path: str | None = None):
        self.name = name
        self.lock_path = lock_path
        self._handle = None  # Windows mutex handle
        self._fh = None      # Linux/macOS lock file handle

    def acquire(self) -> None:
        if sys.platform.startswith("win"):
            self._acquire_windows()
        else:
            self._acquire_flock()

    def _acquire_windows(self) -> None:
        import win32api
        import win32event
        import winerror
        handle = win32event.CreateMutex(None, False, self.name)
        already = win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS
        if already:
            win32api.CloseHandle(handle)
            raise AlreadyRunning(f"mutex {self.name!r} is already held")
        self._handle = handle

    def _acquire_flock(self) -> None:
        import fcntl
        if not self.lock_path:
            raise ValueError("lock_path is required on non-Windows platforms")
        os.makedirs(os.path.dirname(self.lock_path) or ".", exist_ok=True)
        fh = open(self.lock_path, "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise AlreadyRunning(f"lock file {self.lock_path!r} is already held") from exc
        self._fh = fh

    def release(self) -> None:
        if self._handle is not None:
            import win32api
            with contextlib.suppress(Exception):
                win32api.CloseHandle(self._handle)
            self._handle = None
        if self._fh is not None:
            import fcntl
            with contextlib.suppress(Exception):
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            with contextlib.suppress(Exception):
                self._fh.close()
            self._fh = None

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> bool:
        self.release()
        return False
