import os
import threading

import pytest

from tbrprint import api as apimod
from tbrprint import config as cfgmod
from tbrprint import pipeline as pl
from tbrprint.backends import PrintError
from tbrprint.store import Store


class FakeBackend:
    __name__ = "tbrprint.backends.cups"
    PrintError = PrintError

    def __init__(self, fail_times=0):
        self.calls = []
        self.fail_times = fail_times

    def list_printers(self):
        return ["HP LaserJet"]

    def submit(self, printer, path, *, copies=1, options=None, title=None, timeout=30):
        self.calls.append({"printer": printer, "path": path, "copies": copies, "options": options or [], "title": title})
        if self.fail_times > 0:
            self.fail_times -= 1
            raise PrintError("jam")
        return f"{printer}-{len(self.calls)}"

    def cancel(self, backend_job_id, timeout=10):
        pass


class FakeClient:
    """Stands in for api.Client: records acks, serves a fake PDF."""

    def __init__(self, server, *, fail_download=None):
        self.server = server
        self.acks = []
        self.fail_download = fail_download

    def ack(self, job_uuid, state, error=None):
        self.acks.append((job_uuid, state, error))
        return {}

    def allowed_document_host(self, url):
        return apimod.Client(self.server).allowed_document_host(url)

    def download(self, url, dest, *, timeout_s=120, cancelled=None):
        if not self.allowed_document_host(url):
            raise apimod.DownloadError(f"refusing document URL outside the paired host: {url}")
        if self.fail_download:
            raise self.fail_download
        with open(dest, "wb") as fh:
            fh.write(b"%PDF-1.4 fake")
        return dest


def make(tmp_path, *, routing=None, backend=None, client=None, dry_run=False):
    cfg = cfgmod.from_dict({
        "server": {"url": "https://shop.techbenchhub.co.uk", "token": "t", "agent_uuid": "u"},
        "printers": {"lj": {"name": "HP LaserJet"}},
        "routing": routing if routing is not None else {"ticket_label": {"printer": "lj"}},
    })
    store = Store(":memory:")
    backend = backend or FakeBackend()
    client = client or FakeClient(cfg.server)
    pipe = pl.Pipeline(cfg, store, client, spool_dir=str(tmp_path / "spool"), backend=backend, dry_run=dry_run)
    return cfg, store, backend, client, pipe


def wire(uuid="job-0001-uuid", doc_type="ticket_label", copies=1, url=None):
    return {"uuid": uuid, "document_type": doc_type, "title": f"Ticket label #{uuid[-4:]}", "copies": copies,
            "document_url": url or f"https://shop.techbenchhub.co.uk/api/print/v1/jobs/{uuid}/document",
            "status": "queued", "origin": "manual", "created_at": "2026-09-01T10:00:00Z"}


def run_one(pipe, job):
    pipe.submit(job)
    pipe.start()
    pipe.stop()


def test_job_from_wire_validation():
    job = pl.job_from_wire(wire(copies=3))
    assert job.uuid == "job-0001-uuid" and job.copies == 3 and job.document_type == "ticket_label"
    with pytest.raises(pl.PayloadError):
        pl.job_from_wire({"document_type": "x", "document_url": "https://a/b"})
    with pytest.raises(pl.PayloadError):
        pl.job_from_wire(wire(copies=0))
    with pytest.raises(pl.PayloadError):
        pl.job_from_wire({"uuid": "job-0001-uuid", "document_type": "ticket_label"})


def test_prints_and_acks_received_then_printed(tmp_path):
    cfg, store, backend, client, pipe = make(tmp_path)
    run_one(pipe, pl.job_from_wire(wire(copies=2)))
    assert [a[1] for a in client.acks] == ["received", "printed"]
    assert backend.calls[0]["printer"] == "HP LaserJet"
    assert backend.calls[0]["copies"] == 2
    assert "sides=one-sided" in backend.calls[0]["options"]
    row = store.get_job("job-0001-uuid")
    assert row["status"] == "printed" and row["printer"] == "HP LaserJet"


def test_duplicate_uuid_is_dropped_and_not_re_acked(tmp_path):
    cfg, store, backend, client, pipe = make(tmp_path)
    job = pl.job_from_wire(wire())
    assert pipe.submit(job) is True
    assert pipe.submit(pl.job_from_wire(wire())) is False
    pipe.start()
    pipe.stop()
    assert len(backend.calls) == 1
    assert client.acks.count(("job-0001-uuid", "received", None)) == 1


def test_no_route_fails_the_job_with_a_visible_reason(tmp_path):
    cfg, store, backend, client, pipe = make(tmp_path, routing={})
    run_one(pipe, pl.job_from_wire(wire()))
    assert backend.calls == []
    assert client.acks[-1][1] == "failed"
    assert "no printer routed for ticket_label" in client.acks[-1][2]
    assert store.get_job("job-0001-uuid")["status"] == "skipped"


def test_route_copies_override_job_copies(tmp_path):
    cfg, store, backend, client, pipe = make(tmp_path, routing={"ticket_label": {"printer": "lj", "copies": 3}})
    run_one(pipe, pl.job_from_wire(wire(copies=1)))
    assert backend.calls[0]["copies"] == 3


def test_refuses_document_url_off_the_paired_host(tmp_path):
    cfg, store, backend, client, pipe = make(tmp_path)
    run_one(pipe, pl.job_from_wire(wire(url="https://evil.example.com/x.pdf")))
    assert backend.calls == []
    assert client.acks[-1][1] == "failed" and "refusing" in client.acks[-1][2]


def test_print_retries_then_fails_with_reason(tmp_path):
    cfg, store, backend, client, pipe = make(tmp_path, backend=FakeBackend(fail_times=5))
    pipe.cfg.timeouts.print_submit_s = 1
    run_one(pipe, pl.job_from_wire(wire()))
    assert len(backend.calls) == 3
    assert client.acks[-1][1] == "failed" and "jam" in client.acks[-1][2]


def test_download_failure_acks_failed(tmp_path):
    cfg = cfgmod.from_dict({"server": {"url": "https://shop.techbenchhub.co.uk", "token": "t", "agent_uuid": "u"},
                            "printers": {"lj": {"name": "HP LaserJet"}}, "routing": {"ticket_label": {"printer": "lj"}}})
    client = FakeClient(cfg.server, fail_download=apimod.DownloadError("the server cannot render this document any more"))
    cfg, store, backend, client, pipe = make(tmp_path, client=client)
    run_one(pipe, pl.job_from_wire(wire()))
    assert client.acks[-1][1] == "failed" and "cannot render" in client.acks[-1][2]


def test_pause_holds_jobs_until_resume(tmp_path):
    cfg, store, backend, client, pipe = make(tmp_path)
    pipe.pause()
    pipe.submit(pl.job_from_wire(wire()))
    pipe.start()
    assert backend.calls == []
    assert store.get_job("job-0001-uuid")["status"] == "queued"
    pipe.resume()
    pipe.stop()
    assert len(backend.calls) == 1


def test_dry_run_acks_printed_without_calling_backend(tmp_path):
    cfg, store, backend, client, pipe = make(tmp_path, dry_run=True)
    run_one(pipe, pl.job_from_wire(wire()))
    assert backend.calls == []
    assert client.acks[-1][1] == "printed"


def test_reprint_reuses_spool_and_is_local_only(tmp_path):
    cfg, store, backend, client, pipe = make(tmp_path)
    run_one(pipe, pl.job_from_wire(wire()))
    acks_before = len(client.acks)
    pipe2 = pl.Pipeline(cfg, store, client, spool_dir=str(tmp_path / "spool"), backend=backend)
    assert pipe2.reprint("job-0001-uuid") is True
    pipe2.start()
    pipe2.stop()
    assert len(backend.calls) == 2
    assert len(client.acks) == acks_before  # the server is not told about a local reprint


def test_test_print_writes_and_submits(tmp_path):
    cfg, store, backend, client, pipe = make(tmp_path)
    assert pipe.test_print("lj") is True
    assert backend.calls[0]["title"] == "TBRprint test page"
    assert os.path.exists(backend.calls[0]["path"])
    assert pipe.test_print("nope") is False
