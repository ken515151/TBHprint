"""Pure view-model helpers for the applet - everything the windows compute
that is worth unit-testing without a display: menu labels, status text,
and turning the Settings form into a `set_config` payload.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..pipeline import DOCUMENT_TYPES

DOCUMENT_LABELS = {
    "ticket_label": "Ticket label",
    "booking_sheet": "Booking-in sheet",
    "collection_form": "Collection form",
    "invoice": "Invoice",
    "receipt": "Receipt",
    "estimate": "Estimate",
    "credit_note": "Credit note",
    "purchase_order": "Purchase order",
}

STATE_LABELS = {
    "connected": "Connected",
    "degraded": "Polling (websocket down)",
    "disconnected": "Disconnected",
    "error": "Error - see log",
    "paused": "Paused",
    "unpaired": "Not paired",
    "starting": "Starting…",
}


def state_label(status: dict[str, Any] | None) -> str:
    if status is None:
        return "Agent not running"
    label = STATE_LABELS.get(str(status.get("state")), str(status.get("state")))
    active = int(status.get("active_jobs") or 0)
    if active:
        label += f" · {active} job{'s' if active != 1 else ''} in progress"
    if status.get("stuck_jobs"):
        label += " · STUCK"
    if status.get("dry_run"):
        label += " · dry run"
    return label


def tooltip(status: dict[str, Any] | None) -> str:
    if status is None:
        return "TBHprint - agent not running"
    agent = status.get("agent") or "TBHprint"
    return f"TBHprint · {agent} · {state_label(status)}"


def fmt_time(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return iso
    today = datetime.now(timezone.utc).astimezone().date()
    return dt.strftime("%H:%M:%S") if dt.date() == today else dt.strftime("%d %b %H:%M")


def job_row(job: dict[str, Any]) -> tuple[str, str, str, str, str]:
    """(time, document, printer, status, detail) for the history table."""
    doc = job.get("title") or DOCUMENT_LABELS.get(str(job.get("document_type")), str(job.get("document_type")))
    detail = job.get("error") or ""
    copies = int(job.get("copies") or 0)
    if copies > 1 and not detail:
        detail = f"x{copies}"
    return (fmt_time(job.get("received_at")), str(doc), str(job.get("printer") or ""), str(job.get("status")), str(detail))


def printer_key(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name.strip().lower()).strip("_") or "printer"


def routing_rows(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """One editable row per document type from a redacted config dict."""
    printers = cfg.get("printers") or {}
    routing = cfg.get("routing") or {}
    rows = []
    for doc_type in DOCUMENT_TYPES:
        route = routing.get(doc_type) or {}
        printer = printers.get(route.get("printer") or "") or {}
        rows.append({
            "document_type": doc_type,
            "label": DOCUMENT_LABELS.get(doc_type, doc_type),
            "printer_name": printer.get("name") or "",
            "enabled": bool(route.get("enabled", True)) if route else False,
            "copies": route.get("copies"),
        })
    return rows


def routing_update(rows: list[dict[str, Any]], existing: dict[str, Any] | None = None,
                   default_printer_name: str | None = None) -> dict[str, Any]:
    """Settings form rows -> the `printers` + `routing` sections for set_config.
    A row with no printer chosen drops its route; printers are keyed by a
    slug of their OS name so the config stays readable."""
    printers: dict[str, Any] = {}
    routing: dict[str, Any] = {}
    old_printers = (existing or {}).get("printers") or {}
    for row in rows:
        name = (row.get("printer_name") or "").strip()
        if not name:
            continue
        key = printer_key(name)
        printers.setdefault(key, {"name": name, "options": list((old_printers.get(key) or {}).get("options") or [])})
        copies = row.get("copies")
        routing[row["document_type"]] = {
            "printer": key,
            "enabled": bool(row.get("enabled", True)),
            "copies": int(copies) if copies not in (None, "", 0, "0") else None,
            "duplex": "off",
            "rotate": False,
        }
    update: dict[str, Any] = {"printers": printers, "routing": routing}
    if default_printer_name:
        key = printer_key(default_printer_name)
        printers.setdefault(key, {"name": default_printer_name, "options": []})
        update["default_printer"] = key
    else:
        update["default_printer"] = None
    return update
