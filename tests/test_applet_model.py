from tbhprint.applet import icons
from tbhprint.applet.model import (job_row, printer_key, routing_rows, routing_update,
                                   state_label, tooltip)


def test_state_label_and_tooltip():
    assert state_label(None) == "Agent not running"
    assert state_label({"state": "connected"}) == "Connected"
    assert state_label({"state": "degraded", "active_jobs": 2}) == "Polling (websocket down) · 2 jobs in progress"
    assert "STUCK" in state_label({"state": "connected", "stuck_jobs": ["a"]})
    assert "dry run" in state_label({"state": "connected", "dry_run": True})
    assert tooltip({"state": "connected", "agent": "Front desk"}) == "TBHprint · Front desk · Connected"


def test_job_row_prefers_error_then_copies():
    job = {"received_at": "2026-09-01T10:00:00+00:00", "title": "Ticket label #1", "printer": "QL", "status": "failed",
           "error": "no printer routed", "copies": 3}
    assert job_row(job)[1:] == ("Ticket label #1", "QL", "failed", "no printer routed")
    job.update({"status": "printed", "error": None})
    assert job_row(job)[4] == "x3"


def test_routing_rows_and_update_round_trip():
    cfg = {"printers": {"ql_800": {"name": "QL-800", "options": ["fit"]}},
           "routing": {"ticket_label": {"printer": "ql_800", "enabled": True, "copies": 2}}}
    rows = routing_rows(cfg)
    label = next(r for r in rows if r["document_type"] == "ticket_label")
    assert label["printer_name"] == "QL-800" and label["copies"] == 2 and label["enabled"]
    invoice = next(r for r in rows if r["document_type"] == "invoice")
    assert invoice["printer_name"] == "" and invoice["enabled"] is False

    rows[0]["printer_name"] = "QL-800"
    update = routing_update([
        {"document_type": "ticket_label", "printer_name": "QL-800", "copies": 2, "enabled": True},
        {"document_type": "invoice", "printer_name": "HP LaserJet", "copies": "", "enabled": True},
        {"document_type": "estimate", "printer_name": "", "copies": None, "enabled": True},
    ], cfg, default_printer_name="HP LaserJet")
    assert update["printers"]["ql_800"]["options"] == ["fit"]        # existing options kept
    assert update["routing"]["ticket_label"]["copies"] == 2
    assert update["routing"]["invoice"]["copies"] is None
    assert "estimate" not in update["routing"]                        # blank printer drops the route
    assert update["default_printer"] == printer_key("HP LaserJet")


def test_icons_render_every_state():
    for state in ("connected", "degraded", "paused", "error", "unpaired", "starting", "whatever"):
        image = icons.render(state, size=32, badge=3)
        assert image.size == (32, 32)
