"""The long-running agent: wires config, store, pipeline and transports.

Lifecycle: load config -> open SQLite -> control channel -> pipeline ->
transports (Reverb primary, poller catch-up/fallback) -> run until stopped.
"""

from __future__ import annotations

import collections
import logging
import os
import threading
from typing import Any

from . import __version__, api as apimod, config as cfgmod
from .backends import PrintError, get_backend
from .pipeline import PayloadError, Pipeline, job_from_wire
from .store import Store
from .transport_poll import PollerTransport
from .transport_reverb import ReverbTransport

log = logging.getLogger("tbhprint.daemon")


class RingBufferHandler(logging.Handler):
    def __init__(self, capacity: int = 500):
        super().__init__()
        self.records: collections.deque[str] = collections.deque(maxlen=capacity)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(self.format(record))


class Daemon:
    def __init__(self, cfg: cfgmod.Config, store: Store, pipeline: Pipeline, config_path: str):
        self.cfg = cfg
        self.store = store
        self.pipeline = pipeline
        self.config_path = config_path
        self.client: apimod.Client | None = apimod.Client(cfg.server) if cfg.server.is_paired else None
        self.pipeline.set_client(self.client)
        # unpaired | starting | connected | degraded | disconnected | error
        self.state = "starting" if cfg.server.is_paired else "unpaired"
        self.transport_name = "none"
        self.log_handler = RingBufferHandler()
        logging.getLogger().addHandler(self.log_handler)
        self.reverb: ReverbTransport | None = None
        self.poller: PollerTransport | None = None
        self._stop_event = threading.Event()

    # -- transports -------------------------------------------------------

    def start_transports(self) -> None:
        if not self.cfg.server.is_paired:
            log.info("not paired yet - run `tbhprint pair <server url> <code>`")
            self.state = "unpaired"
            return
        mode = self.cfg.transport.mode
        self.poller = PollerTransport(
            client_provider=lambda: self.client,
            on_job=self._on_wire_payload,
            interval_s=self.cfg.transport.poll_interval_s,
            on_state=self._on_transport_state,
        )
        self.poller.start()
        reverb = self.cfg.server.reverb
        if mode in ("auto", "realtime") and reverb.key and reverb.host:
            self.reverb = ReverbTransport(
                ws_url=reverb.ws_url,
                channel=self.cfg.server.channel,
                auth_provider=self._channel_auth,
                on_job=self._on_wire_payload,
                on_connect=self._on_reverb_connect,
                on_state=self._on_transport_state,
            )
            self.reverb.start()
            if mode == "auto":
                self.poller.set_active(True)
        else:
            if mode != "poll":
                log.warning("server sent no websocket details - polling only")
            self.transport_name = "poller"
            self.poller.set_active(True)
        # Always one catch-up at start so nothing queued while we were down is missed.
        threading.Thread(target=self._safe_catch_up, name="startup-catch-up", daemon=True).start()

    def _channel_auth(self, socket_id: str, channel: str) -> str:
        if self.client is None:
            raise RuntimeError("not paired")
        return self.client.channel_auth(socket_id, channel)

    def _on_reverb_connect(self) -> None:
        self.transport_name = "reverb"
        self.state = "connected"
        self._safe_catch_up()
        if self.cfg.transport.mode == "auto" and self.poller:
            self.poller.set_active(False)

    def _safe_catch_up(self) -> None:
        try:
            if self.poller:
                self.poller.sweep_once()
        except apimod.AuthError as exc:
            log.error("%s", exc)
            self.state = "error"
        except Exception:
            log.exception("catch-up sweep failed")

    def _on_transport_state(self, state: str) -> None:
        if state == "connected":
            self.state = "connected"
        elif state == "disconnected":
            self.state = "degraded" if self.cfg.transport.mode == "auto" else "disconnected"
            if self.cfg.transport.mode == "auto" and self.poller:
                self.poller.set_active(True)
        elif state in ("degraded", "error"):
            if self.state != "connected":
                self.state = state

    def _on_wire_payload(self, payload: dict[str, Any]) -> None:
        try:
            job = job_from_wire(payload)
        except PayloadError as exc:
            log.warning("dropping malformed payload: %s", exc)
            return
        self.pipeline.submit(job)

    # -- control surface -----------------------------------------------------

    def status(self) -> dict[str, Any]:
        active = self.store.active_jobs()
        stuck = self.store.stuck_jobs(self.cfg.timeouts.stuck_flag_s)
        return {
            "version": __version__,
            "state": "paused" if self.pipeline.paused else self.state,
            "transport": self.transport_name,
            "paired": self.cfg.server.is_paired,
            "server": self.cfg.server.url,
            "agent": self.cfg.server.agent_name,
            "paused": self.pipeline.paused,
            "dry_run": self.pipeline.dry_run,
            "active_jobs": len(active),
            "stuck_jobs": [j["job_id"] for j in stuck],
        }

    def log_tail(self, n: int) -> list[str]:
        return list(self.log_handler.records)[-n:]

    def list_system_printers(self) -> list[str]:
        try:
            return self.pipeline.backend.list_printers()
        except PrintError as exc:
            log.warning("cannot list printers: %s", exc)
            return []

    def catch_up(self) -> int:
        return self.poller.sweep_once() if self.poller else 0

    def update_config(self, update: dict[str, Any]) -> None:
        new_cfg = cfgmod.apply_update(self.cfg, update)
        cfgmod.save(new_cfg, self.config_path)
        old_server = self.cfg.server.to_dict() if hasattr(self.cfg.server, "to_dict") else vars(self.cfg.server)
        self.cfg = new_cfg
        self.pipeline.set_config(new_cfg)
        if self.poller:
            self.poller.interval_s = new_cfg.transport.poll_interval_s
        if vars(new_cfg.server) != old_server:
            self.restart_transports()
        log.info("config updated and saved")

    def reload_config(self) -> None:
        new_cfg = cfgmod.load(self.config_path)
        changed = vars(new_cfg.server) != vars(self.cfg.server)
        self.cfg = new_cfg
        self.pipeline.set_config(new_cfg)
        if changed:
            self.restart_transports()
        log.info("config reloaded from disk")

    def restart_transports(self) -> None:
        for transport in (self.reverb, self.poller):
            if transport:
                transport.stop()
        self.reverb = self.poller = None
        self.client = apimod.Client(self.cfg.server) if self.cfg.server.is_paired else None
        self.pipeline.set_client(self.client)
        self.transport_name = "none"
        self.state = "starting" if self.cfg.server.is_paired else "unpaired"
        self.start_transports()

    # -- maintenance -------------------------------------------------------

    def maintenance_loop(self) -> None:
        while not self._stop_event.wait(timeout=3600):
            try:
                self.pipeline.retention_sweep()
            except Exception:
                log.exception("retention sweep failed")

    def stop(self) -> None:
        self._stop_event.set()
        for transport in (self.reverb, self.poller):
            if transport:
                transport.stop()
        self.pipeline.stop()


def build(config_path: str, *, state_dir: str | None = None, dry_run: bool = False,
          backend_name: str | None = None) -> tuple[Daemon, Store, Pipeline]:
    """Construct the daemon from a config path (creating an unpaired config if absent)."""
    try:
        cfg = cfgmod.load(config_path)
    except cfgmod.ConfigError as exc:
        if "not found" not in str(exc):
            raise
        log.warning("no config file at %s - starting unpaired", config_path)
        cfg = cfgmod.Config()
        try:
            cfgmod.save(cfg, config_path)
        except OSError as save_exc:
            log.warning("could not create config file: %s", save_exc)
    state_dir = state_dir or cfgmod.default_state_dir()
    os.makedirs(state_dir, exist_ok=True)
    store = Store(os.path.join(state_dir, "jobs.db"))
    backend = get_backend(backend_name or cfg.backend)
    pipeline = Pipeline(cfg, store, None, spool_dir=os.path.join(state_dir, "spool"),
                        backend=backend, dry_run=dry_run)
    daemon = Daemon(cfg, store, pipeline, config_path=config_path)
    return daemon, store, pipeline
