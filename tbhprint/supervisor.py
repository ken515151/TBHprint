"""Windows process supervisor: the tray owns the agent as a child process
and keeps it alive. Linux does not use this - systemd supervises the
daemon there (see packaging/tbhprint.service); the tray is only ever a
control-channel client on Linux.

Two independent failure modes are handled:

  - the child exits (crash, or someone kills it) -> restart with
    exponential backoff (1, 2, 4 ... capped at 30s), forever.
  - the child is alive but wedged: the watchdog polls the agent's own
    control channel (`status`) and, if nothing has answered for
    `watchdog_timeout` seconds, kills the child so the restart loop above
    picks it back up.

`spawn` and `check_alive` are injected so tests can supply a fake child
process and a fake liveness check instead of a real one.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from typing import Callable, Protocol

log = logging.getLogger("tbhprint.supervisor")

BACKOFF_START_S = 1.0
BACKOFF_CAP_S = 30.0
WATCHDOG_INTERVAL_S = 10.0
WATCHDOG_TIMEOUT_S = 60.0
LOG_ROTATE_MAX_BYTES = 2 * 1024 * 1024
LOG_ROTATE_BACKUPS = 5


class ChildProcess(Protocol):
    """The slice of `subprocess.Popen` the supervisor needs - a Protocol so
    tests can hand it a fake without subclassing Popen."""

    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


def rotate_log_if_large(path: str, max_bytes: int = LOG_ROTATE_MAX_BYTES,
                        backups: int = LOG_ROTATE_BACKUPS) -> None:
    """Cheap size-based rotation, checked once per (re)start - the child's
    stdout/stderr are redirected straight to `path` by the supervisor, so
    this is the only place that ever needs to rotate it."""
    try:
        if not os.path.exists(path) or os.path.getsize(path) < max_bytes:
            return
    except OSError:
        return
    for i in range(backups - 1, 0, -1):
        src, dst = f"{path}.{i}", f"{path}.{i + 1}"
        if os.path.exists(src):
            with contextlib.suppress(OSError):
                os.replace(src, dst)
    with contextlib.suppress(OSError):
        os.replace(path, f"{path}.1")


class Supervisor:
    def __init__(self, spawn: Callable[[], ChildProcess], *, check_alive: Callable[[], bool],
                backoff_start: float = BACKOFF_START_S, backoff_cap: float = BACKOFF_CAP_S,
                watchdog_interval: float = WATCHDOG_INTERVAL_S, watchdog_timeout: float = WATCHDOG_TIMEOUT_S):
        """`spawn()` launches one child and returns it. `check_alive()` asks
        the agent's own control channel for `status` and returns whether it
        answered in time - the watchdog's "has anyone answered?" test."""
        self.spawn = spawn
        self.check_alive = check_alive
        self.backoff_start = backoff_start
        self.backoff_cap = backoff_cap
        self.watchdog_interval = watchdog_interval
        self.watchdog_timeout = watchdog_timeout
        self.child: ChildProcess | None = None
        self._stopping = threading.Event()
        self._restart_thread: threading.Thread | None = None
        self._watchdog_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_answered = time.monotonic()

    def start(self) -> None:
        self._restart_thread = threading.Thread(target=self._restart_loop, name="supervisor", daemon=True)
        self._restart_thread.start()
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, name="watchdog", daemon=True)
        self._watchdog_thread.start()

    def stop(self) -> None:
        self._stopping.set()
        with self._lock:
            child = self.child
        if child is not None:
            self._kill(child)
        if self._restart_thread:
            self._restart_thread.join(timeout=10)
        if self._watchdog_thread:
            self._watchdog_thread.join(timeout=10)

    def _restart_loop(self) -> None:
        backoff = self.backoff_start
        while not self._stopping.is_set():
            try:
                with self._lock:
                    self.child = self.spawn()
            except Exception:
                # A failed launch (missing interpreter after a broken update,
                # locked log file, ...) must not end supervision: log it and
                # keep trying on the same backoff as a crash.
                log.exception("could not start the agent child - retrying in %.0fs", backoff)
                if self._stopping.wait(backoff):
                    break
                backoff = min(backoff * 2, self.backoff_cap)
                continue
            child = self.child
            log.info("agent child started (pid %s)", getattr(child, "pid", "?"))
            self._last_answered = time.monotonic()
            backoff = self.backoff_start
            while not self._stopping.is_set() and child.poll() is None:
                self._stopping.wait(0.5)
            if self._stopping.is_set():
                break
            log.warning("agent child exited (code %s) - restarting in %.0fs", child.poll(), backoff)
            if self._stopping.wait(backoff):
                break
            backoff = min(backoff * 2, self.backoff_cap)

    def _watchdog_loop(self) -> None:
        while not self._stopping.wait(self.watchdog_interval):
            with self._lock:
                child = self.child
            if child is None or child.poll() is not None:
                continue  # nothing to watch, or the restart loop already knows it exited
            try:
                answered = self.check_alive()
            except Exception:
                log.debug("watchdog liveness check raised", exc_info=True)
                answered = False
            now = time.monotonic()
            if answered:
                self._last_answered = now
                continue
            if now - self._last_answered >= self.watchdog_timeout:
                log.warning("agent has not answered `status` for %.0fs - killing and restarting",
                           self.watchdog_timeout)
                self._kill(child)
                self._last_answered = now

    @staticmethod
    def _kill(child: ChildProcess) -> None:
        with contextlib.suppress(Exception):
            child.terminate()
        try:
            child.wait(timeout=5)
        except Exception:
            with contextlib.suppress(Exception):
                child.kill()
