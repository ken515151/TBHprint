"""Realtime transport: Laravel Reverb over the Pusher protocol.

Reverb speaks the Pusher Channels protocol, so this is the SyncroPrint
Pusher client with one addition - PRIVATE channel subscription: after
`pusher:connection_established` we ask the TechBenchHub print API to sign
(socket_id, channel) with our bearer token and send that `auth` string in
`pusher:subscribe`. Only the agent whose token it is can subscribe to its
own channel (routes/channels.php on the server), which is the fix for
AutoPrintr's public broadcast channel.

Runs its own asyncio loop on a daemon thread; callbacks fire there.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import threading
from typing import Any, Callable

import websockets

from . import __version__

log = logging.getLogger("tbhprint.reverb")

PROTOCOL_VERSION = 7
PRINT_JOB_EVENT = "print.job"

_BACKOFF_START = 1.0
_BACKOFF_CAP = 60.0
_DEFAULT_ACTIVITY_TIMEOUT = 120
_PONG_TIMEOUT = 30


class ReverbTransport:
    """on_job(payload)   - a print.job event arrived
    on_connect()          - (re)connected + subscribed; the daemon runs the catch-up poll here
    on_state(state)       - "connected" | "disconnected"
    auth_provider(socket_id, channel) -> auth string (blocking; called on the transport thread)
    """

    def __init__(self, ws_url: str, channel: str,
                 auth_provider: Callable[[str, str], str],
                 on_job: Callable[[dict[str, Any]], None],
                 on_connect: Callable[[], None] = lambda: None,
                 on_state: Callable[[str], None] = lambda s: None):
        self.ws_url = ws_url
        self.channel = channel
        self.auth_provider = auth_provider
        self.on_job = on_job
        self.on_connect = on_connect
        self.on_state = on_state
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._stopping = threading.Event()

    @property
    def url(self) -> str:
        return f"{self.ws_url}?protocol={PROTOCOL_VERSION}&client=tbhprint&version={__version__}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._thread_main, name="reverb", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        loop, task = self._loop, self._task
        if loop and loop.is_running() and task:
            loop.call_soon_threadsafe(task.cancel)
        if self._thread:
            self._thread.join(timeout=10)

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._task = self._loop.create_task(self._run())
            self._loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass
        finally:
            self._loop.close()

    async def _run(self) -> None:
        backoff = _BACKOFF_START
        while not self._stopping.is_set():
            try:
                await self._session()
                backoff = _BACKOFF_START
            except Exception as exc:
                log.warning("reverb connection failed: %s", exc)
            if self._stopping.is_set():
                break
            self.on_state("disconnected")
            delay = backoff + random.uniform(0, backoff / 2)
            log.info("reconnecting in %.1fs", delay)
            try:
                await asyncio.wait_for(asyncio.to_thread(self._stopping.wait, delay), delay + 1)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, _BACKOFF_CAP)

    async def _session(self) -> None:
        async with websockets.connect(self.url, open_timeout=20, close_timeout=5,
                                      max_size=1 << 20) as ws:
            activity_timeout = await self._handshake(ws)
            log.info("connected and subscribed to %s", self.channel)
            self.on_state("connected")
            self.on_connect()
            await self._read_loop(ws, activity_timeout)

    async def _handshake(self, ws) -> int:
        raw = await asyncio.wait_for(ws.recv(), timeout=20)
        msg = json.loads(raw)
        if msg.get("event") != "pusher:connection_established":
            raise RuntimeError(f"expected connection_established, got {msg.get('event')!r}")
        data = _event_data(msg)
        socket_id = str(data.get("socket_id") or "")
        if not socket_id:
            raise RuntimeError("no socket_id in connection_established")
        activity_timeout = int(data.get("activity_timeout", _DEFAULT_ACTIVITY_TIMEOUT))
        auth = await asyncio.to_thread(self.auth_provider, socket_id, self.channel)
        await ws.send(json.dumps({"event": "pusher:subscribe",
                                  "data": {"channel": self.channel, "auth": auth}}))
        # Wait for the subscription result so an auth failure is loud, not silent.
        raw = await asyncio.wait_for(ws.recv(), timeout=20)
        msg = json.loads(raw)
        if msg.get("event") == "pusher:error" or (msg.get("event") == "pusher_internal:subscription_error"):
            raise RuntimeError(f"subscription refused: {msg.get('data')}")
        if msg.get("event") != "pusher_internal:subscription_succeeded":
            # Not the confirmation - dispatch it and carry on; confirmation may follow.
            self._dispatch(msg, ws)
        return activity_timeout

    async def _read_loop(self, ws, activity_timeout: int) -> None:
        while not self._stopping.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=activity_timeout)
            except asyncio.TimeoutError:
                await ws.send(json.dumps({"event": "pusher:ping", "data": {}}))
                raw = await asyncio.wait_for(ws.recv(), timeout=_PONG_TIMEOUT)
            self._dispatch(json.loads(raw), ws)

    def _dispatch(self, msg: dict[str, Any], ws) -> None:
        event = msg.get("event", "")
        if event == "pusher:ping":
            asyncio.ensure_future(ws.send(json.dumps({"event": "pusher:pong", "data": {}})))
        elif event == PRINT_JOB_EVENT:
            try:
                payload = _event_data(msg)
            except (ValueError, TypeError) as exc:
                log.warning("undecodable print.job event: %s", exc)
                return
            log.info("print.job event received")
            self.on_job(payload)
        elif event == "pusher:error":
            data = msg.get("data") or {}
            log.error("reverb error %s: %s", data.get("code"), data.get("message"))


def _event_data(msg: dict[str, Any]) -> dict[str, Any]:
    """Pusher double-encodes event data as a JSON string; tolerate both."""
    data = msg.get("data", {})
    if isinstance(data, str):
        data = json.loads(data) if data else {}
    if not isinstance(data, dict):
        raise TypeError(f"event data is {type(data).__name__}, expected object")
    return data
