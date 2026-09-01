"""Poller transport: catch-up + fallback.

Unlike AutoPrintr, TechBenchHub HAS a job-list endpoint: `GET /jobs`
returns every job still open for this agent. So the poller is exact, not
best-effort - one sweep on every websocket reconnect covers any outage,
and timed sweeps keep printing working when the websocket is down.
Dedupe by server uuid makes overlapping sweeps harmless.

The poller never fully stops: while the websocket is CONNECTED it keeps
sweeping at the slower heartbeat cadence, both to keep the server's
last_seen fresh (a silent websocket agent otherwise shows "offline" in
the shop after five minutes) and to recover any broadcast the socket
dropped without a reconnect.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from . import api as apimod

log = logging.getLogger("tbhprint.poll")

CURSOR_KEY = "poll_cursor"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class PollerTransport:
    def __init__(self, client_provider: Callable[[], apimod.Client | None],
                 on_job: Callable[[dict[str, Any]], None],
                 interval_s: int = 60,
                 heartbeat_s: int = 120,
                 on_state: Callable[[str], None] = lambda s: None):
        self.client_provider = client_provider
        self.on_job = on_job
        self.interval_s = interval_s
        self.heartbeat_s = heartbeat_s
        self.on_state = on_state
        self.active = False
        # 'fallback': websocket down - sweep every interval_s and report
        # transport state. 'heartbeat': websocket UP - keep sweeping, slower
        # (heartbeat_s), and stay silent about state (the websocket owns it).
        # The heartbeat exists for two reasons found live on 2026-09-01:
        # a connected agent otherwise makes NO http calls, so the server's
        # last_seen ages out and the shop sees it "offline" (print rows
        # hide); and a broadcast the websocket dropped would wait until the
        # next reconnect - the sweep picks it up within heartbeat_s.
        self.mode = "fallback"
        self._wake = threading.Event()
        self._stopping = False
        self._thread: threading.Thread | None = None
        self._sweep_lock = threading.Lock()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=10)

    def set_active(self, active: bool) -> None:
        if active and not self.active:
            log.info("poller activated")
        self.active = active
        if active:
            self._wake.set()

    def set_mode(self, mode: str) -> None:
        """'fallback' (websocket down) or 'heartbeat' (websocket up). Both
        keep the poller ACTIVE - the agent never goes silent on the server."""
        if mode not in ("fallback", "heartbeat"):
            raise ValueError(f"unknown poller mode {mode!r}")
        if mode != self.mode:
            log.info("poller mode: %s (every %ds)",
                     mode, self.heartbeat_s if mode == "heartbeat" else self.interval_s)
        self.mode = mode
        self.active = True
        self._wake.set()

    def sweep_once(self) -> int:
        """One GET /jobs; hands every open job to on_job. Returns the count."""
        with self._sweep_lock:
            client = self.client_provider()
            if client is None:
                return 0
            jobs = client.list_jobs()
            for payload in jobs:
                self.on_job(payload)
            if jobs:
                log.info("catch-up found %d open job(s)", len(jobs))
            return len(jobs)

    def _run(self) -> None:
        while not self._stopping:
            timeout = self.heartbeat_s if self.mode == "heartbeat" else self.interval_s
            self._wake.wait(timeout=timeout)
            self._wake.clear()
            if self._stopping or not self.active:
                continue
            quiet = self.mode == "heartbeat"
            try:
                self.sweep_once()
                if not quiet:
                    self.on_state("degraded")
            except apimod.AuthError as exc:
                log.error("%s", exc)
                if not quiet:
                    self.on_state("error")
            except Exception as exc:
                log.warning("poll sweep failed: %s", exc)
                if not quiet:
                    self.on_state("error")
