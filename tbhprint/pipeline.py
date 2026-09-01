"""Job pipeline: dedupe -> ack received -> fetch PDF -> route -> print -> ack.

Transports (Reverb websocket or the poller) hand `Job`s to
`Pipeline.submit()`. One worker thread processes them with per-job
timeouts so a stuck job never blocks the queue. Every outcome is acked to
the server so Settings -> Printing shows the truth.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
import uuid as uuidlib
from dataclasses import dataclass, field
from typing import Any

from . import api as apimod
from .backends import PrintError
from .config import Config
from .store import Store

log = logging.getLogger("tbhprint.pipeline")

DOCUMENT_TYPES = ("invoice", "receipt", "estimate", "credit_note", "purchase_order",
                  "booking_sheet", "collection_form", "ticket_label")


class PayloadError(ValueError):
    """Inbound job payload failed validation - the channel is untrusted input."""


@dataclass
class Job:
    """A print job as the server describes it (docs/PROTOCOL.md "Job JSON")."""
    uuid: str
    document_type: str
    document_url: str | None
    title: str | None = None
    copies: int = 1
    origin: str = "manual"
    raw: dict[str, Any] = field(default_factory=dict)


def job_from_wire(payload: dict[str, Any]) -> Job:
    if not isinstance(payload, dict):
        raise PayloadError("payload is not a JSON object")
    job_uuid = payload.get("uuid")
    doc_type = payload.get("document_type")
    url = payload.get("document_url")
    if not isinstance(job_uuid, str) or not (8 <= len(job_uuid) <= 64):
        raise PayloadError("missing or malformed uuid")
    if not isinstance(doc_type, str) or not doc_type or len(doc_type) > 32:
        raise PayloadError("missing document_type")
    if doc_type not in DOCUMENT_TYPES:
        log.warning("unknown document type %r - routing as-is", doc_type)
    if url is not None and (not isinstance(url, str) or not url):
        raise PayloadError("document_url is not a string")
    if url is None and not payload.get("_reuse_spool"):
        raise PayloadError("job has no document_url")
    copies = payload.get("copies", 1)
    if not isinstance(copies, int) or not (1 <= copies <= 99):
        raise PayloadError(f"copies out of range: {copies!r}")
    title = payload.get("title")
    return Job(
        uuid=job_uuid,
        document_type=doc_type,
        document_url=url,
        title=title if isinstance(title, str) else None,
        copies=copies,
        origin=str(payload.get("origin") or "manual"),
        raw=dict(payload),
    )


class Pipeline:
    def __init__(self, cfg: Config, store: Store, client: apimod.Client | None, *,
                 spool_dir: str, backend, dry_run: bool = False,
                 verify_delay_s: float = 8.0, verify_max_attempts: int = 15):
        self.cfg = cfg
        self.store = store
        self.client = client
        self.spool_dir = spool_dir
        self.backend = backend
        self.dry_run = dry_run
        self.verify_delay_s = verify_delay_s
        self.verify_max_attempts = verify_max_attempts
        self.paused = False
        self._queue: queue.Queue[Job | None] = queue.Queue()
        self._held: list[Job] = []
        self._cancels: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._stopping = False
        os.makedirs(spool_dir, exist_ok=True)

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self._worker = threading.Thread(target=self._run, name="pipeline", daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._stopping = True
        self._queue.put(None)
        if self._worker:
            self._worker.join(timeout=10)

    def set_config(self, cfg: Config) -> None:
        self.cfg = cfg

    def set_client(self, client: apimod.Client | None) -> None:
        self.client = client

    # -- intake -----------------------------------------------------------

    def submit(self, job: Job) -> bool:
        """Dedupe-gate and enqueue. False when the uuid was already seen."""
        if not self.store.add_job(job.uuid, job.document_type, title=job.title,
                                  copies=job.copies, payload=job.raw):
            log.debug("duplicate job %s dropped", job.uuid)
            return False
        self._ack(job.uuid, "received", local_only=bool(job.raw.get("_local_only")))
        with self._lock:
            if self.paused:
                self.store.set_status(job.uuid, "queued")
                self._held.append(job)
                log.info("job %s queued (paused)", job.uuid)
                return True
        self._queue.put(job)
        return True

    def pause(self) -> None:
        with self._lock:
            self.paused = True
        log.info("pipeline paused")

    def resume(self) -> None:
        with self._lock:
            self.paused = False
            held, self._held = self._held, []
        for job in held:
            self._queue.put(job)
        log.info("pipeline resumed, %d held job(s) flushed", len(held))

    def cancel(self, job_uuid: str) -> bool:
        row = self.store.get_job(job_uuid)
        if not row:
            return False
        event = self._cancels.get(job_uuid)
        if event:
            event.set()
        with self._lock:
            self._held = [j for j in self._held if j.uuid != job_uuid]
        if row.get("backend_job_id") and row["status"] == "printing":
            try:
                self.backend.cancel(row["backend_job_id"])
            except PrintError as exc:
                log.warning("backend cancel of %s failed: %s", row["backend_job_id"], exc)
        if row["status"] != "printed":
            self.store.set_status(job_uuid, "cancelled")
            self._ack(job_uuid, "failed", "cancelled on the agent")
        return True

    def reprint(self, job_uuid: str) -> bool:
        """Re-run a job from its retained spool file (else re-fetch). Local only:
        the server sees the original job's status; a server-side reprint is a new job."""
        row = self.store.get_job(job_uuid)
        if not row:
            return False
        spool = row.get("spool_path")
        raw = dict(row.get("payload") or {})
        new_uuid = f"{job_uuid}-reprint-{uuidlib.uuid4().hex[:8]}"
        raw["uuid"] = new_uuid
        raw["_local_only"] = True
        if spool and os.path.exists(spool):
            raw["_reuse_spool"] = spool
        elif not raw.get("document_url"):
            log.warning("reprint %s: no spool file and no recorded URL", job_uuid)
            return False
        job = job_from_wire(raw)
        return self.submit(job)

    def test_print(self, printer_key: str) -> bool:
        printer = self.cfg.printers.get(printer_key)
        if not printer:
            return False
        path = os.path.join(self.spool_dir, "testpage.pdf")
        _write_test_pdf(path)
        job_uuid = f"test-{uuidlib.uuid4().hex[:8]}"
        self.store.add_job(job_uuid, "test_page", title=f"Test page -> {printer.name}", payload={"_local_only": True})
        self._print_file(job_uuid, path, printer.name, 1, list(printer.options), title="TBHprint test page", local_only=True)
        return True

    # -- worker -----------------------------------------------------------

    def _run(self) -> None:
        while not self._stopping:
            job = self._queue.get()
            if job is None:
                break
            try:
                self._process(job)
            except Exception:
                log.exception("unexpected error processing job %s", job.uuid)
                self._fail(job, "internal error on the agent (see its log)")
            finally:
                self._cancels.pop(job.uuid, None)

    def _process(self, job: Job) -> None:
        cancel = threading.Event()
        self._cancels[job.uuid] = cancel

        route = self.cfg.route_for(job.document_type)
        if route is None or not route.enabled:
            reason = (f"no printer routed for {job.document_type} on this agent"
                      if route is None else f"{job.document_type} is disabled on this agent")
            log.info("job %s skipped: %s", job.uuid, reason)
            self.store.set_status(job.uuid, "skipped", error=reason)
            self._ack(job.uuid, "failed", reason, local_only=job.raw.get("_local_only"))
            return
        printer = self.cfg.printer_for(route)
        if printer is None:
            reason = f"printer {route.printer!r} is not configured on this agent"
            self.store.set_status(job.uuid, "skipped", error=reason)
            self._ack(job.uuid, "failed", reason, local_only=job.raw.get("_local_only"))
            return

        reuse = job.raw.get("_reuse_spool")
        if reuse and os.path.exists(reuse):
            path = reuse
        else:
            self.store.set_status(job.uuid, "downloading")
            try:
                path = self._download(job, cancel)
            except apimod.DownloadError as exc:
                if str(exc) == "cancelled":
                    self.store.set_status(job.uuid, "cancelled")
                    return
                log.error("job %s download failed: %s", job.uuid, exc)
                self._fail(job, f"download: {exc}")
                return
            except Exception as exc:
                log.error("job %s download failed: %s", job.uuid, exc)
                self._fail(job, f"download: {exc}")
                return
            self.store.set_status(job.uuid, "downloading", spool_path=path)
        if cancel.is_set():
            self.store.set_status(job.uuid, "cancelled")
            return

        copies = route.copies if route.copies is not None else job.copies
        options = list(printer.options)
        if self.backend.__name__.endswith("cups"):
            options += route.lp_options()
        self._print_file(job.uuid, path, printer.name, copies, options,
                         title=job.title or f"{job.document_type} {job.uuid}",
                         local_only=bool(job.raw.get("_local_only")))

    def _print_file(self, job_uuid: str, path: str, printer_name: str, copies: int,
                    options: list[str], title: str | None = None, local_only: bool = False) -> None:
        self.store.set_status(job_uuid, "printing", printer=printer_name, copies=copies)
        if self.dry_run:
            log.info("[dry-run] would print %s on %s x%d opts=%s", path, printer_name, copies, options)
            self.store.set_status(job_uuid, "printed", error="dry-run")
            self._ack(job_uuid, "printed", local_only=local_only)
            return
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                backend_id = self.backend.submit(
                    printer_name, path, copies=copies, options=options, title=title,
                    timeout=self.cfg.timeouts.print_submit_s)
                self.store.set_status(job_uuid, "printed", backend_job_id=backend_id)
                log.info("job %s printed on %s as %s", job_uuid, printer_name, backend_id)
                self._ack(job_uuid, "printed", local_only=local_only)
                self._schedule_verify(job_uuid, backend_id)
                return
            except PrintError as exc:
                last_error = exc
                log.warning("job %s print attempt %d failed: %s", job_uuid, attempt + 1, exc)
                time.sleep(min(2 ** attempt, 5))
        self.store.set_status(job_uuid, "failed", error=f"print: {last_error}")
        self._ack(job_uuid, "failed", f"print: {last_error}", local_only=local_only)

    def _download(self, job: Job, cancel: threading.Event) -> str:
        if self.client is None:
            raise apimod.DownloadError("agent is not paired")
        dest = os.path.join(self.spool_dir, f"{_safe_name(job.uuid)}.pdf")
        last_error: Exception | None = None
        for attempt in range(3):
            if cancel.is_set():
                raise apimod.DownloadError("cancelled")
            try:
                return self.client.download(job.document_url or "", dest,
                                            timeout_s=self.cfg.timeouts.download_s, cancelled=cancel)
            except apimod.DownloadError as exc:
                # Allowlist / content refusals and 410s are final; retry only network-ish failures.
                message = str(exc)
                if message == "cancelled" or message.startswith(("refusing", "unexpected content-type",
                                                                  "file exceeds", "document is not")):
                    raise
                if "cannot render" in message or "HTTP 4" in message:
                    raise
                last_error = exc
            except apimod.AuthError:
                raise
            except Exception as exc:  # requests.RequestException, OSError
                last_error = exc
            log.warning("job %s download attempt %d failed: %s", job.uuid, attempt + 1, last_error)
            time.sleep(min(2 ** attempt, 8))
        raise apimod.DownloadError(str(last_error))

    def _fail(self, job: Job, reason: str) -> None:
        self.store.set_status(job.uuid, "failed", error=reason)
        self._ack(job.uuid, "failed", reason, local_only=bool(job.raw.get("_local_only")))

    def _ack(self, job_uuid: str, state: str, error: str | None = None, local_only=False) -> None:
        """Tell the server. Never raises - an ack failure is logged, the print still happened."""
        if local_only or self.client is None or job_uuid.startswith("test-"):
            return
        try:
            self.client.ack(job_uuid, state, error)
        except Exception as exc:
            log.warning("ack %s for %s failed: %s", state, job_uuid, exc)

    # -- backend outcome verification ---------------------------------------

    def _schedule_verify(self, job_uuid: str, backend_id: str, attempt: int = 1) -> None:
        if not hasattr(self.backend, "job_outcome"):
            return
        timer = threading.Timer(self.verify_delay_s, self._verify, args=(job_uuid, backend_id, attempt))
        timer.daemon = True
        timer.start()

    def _verify(self, job_uuid: str, backend_id: str, attempt: int) -> None:
        try:
            outcome = self.backend.job_outcome(backend_id)
        except Exception as exc:
            log.debug("verify of %s skipped: %s", backend_id, exc)
            return
        if outcome == "active":
            if attempt < self.verify_max_attempts:
                self._schedule_verify(job_uuid, backend_id, attempt + 1)
            return
        if outcome == "failed":
            row = self.store.get_job(job_uuid)
            if row and row["status"] == "printed":
                reason = f"the print system cancelled/aborted job {backend_id} after accepting it"
                self.store.set_status(job_uuid, "failed", error=reason)
                self._ack(job_uuid, "failed", reason, local_only=bool((row.get("payload") or {}).get("_local_only")))

    # -- maintenance --------------------------------------------------------

    def retention_sweep(self) -> int:
        removed = 0
        for job_uuid, path in self.store.spool_paths_older_than(self.cfg.retention.spool_days):
            try:
                if os.path.exists(path):
                    os.remove(path)
                removed += 1
            except OSError as exc:
                log.warning("could not remove spool file %s: %s", path, exc)
                continue
            self.store.clear_spool_path(job_uuid)
        self.store.delete_older_than(90)
        if removed:
            log.info("retention sweep removed %d spool file(s)", removed)
        return removed


def _safe_name(job_id: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in job_id)[:100]


_MINIMAL_PDF = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj
4 0 obj<</Length 78>>stream
BT /F1 18 Tf 72 770 Td (TBHprint test page) Tj 0 -24 Td (Printing works.) Tj ET
endstream
endobj
5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj
trailer<</Root 1 0 R>>
%%EOF
"""


def _write_test_pdf(path: str) -> None:
    with open(path, "wb") as fh:
        fh.write(_MINIMAL_PDF)
