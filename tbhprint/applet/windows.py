"""The applet's windows (tkinter): Status, History, Settings, Log.

Every window is a Toplevel owned by the hidden root; `refresh()` is
called by the tray's 5-second tick. Long calls (pairing, listing OS
printers) run on a worker thread and post back with `after`.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

from .. import api as apimod
from .. import config as cfgmod
from .. import control
from .model import fmt_time, job_row, routing_rows, routing_update, state_label

PAD = {"padx": 8, "pady": 4}


class _Window:
    title = "TBHprint"
    size = "560x400"

    def __init__(self, app):
        self.app = app
        self.top = tk.Toplevel(app.root)
        self.top.title(self.title)
        self.top.geometry(self.size)
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        self.build()
        self.refresh()

    def build(self) -> None:  # pragma: no cover - subclass hook
        pass

    def refresh(self) -> None:  # pragma: no cover - subclass hook
        pass

    def alive(self) -> bool:
        try:
            return bool(self.top.winfo_exists())
        except tk.TclError:
            return False

    def raise_(self) -> None:
        self.top.deiconify()
        self.top.lift()
        self.top.focus_force()

    def close(self) -> None:
        self.top.destroy()

    def after(self, ms: int, fn) -> None:
        if self.alive():
            self.top.after(ms, fn)


class StatusWindow(_Window):
    title = "TBHprint - Status"
    size = "460x260"

    def build(self) -> None:
        self.vars = {k: tk.StringVar() for k in ("state", "agent", "server", "transport", "active", "version")}
        frame = ttk.Frame(self.top, padding=10)
        frame.pack(fill="both", expand=True)
        for row, (label, key) in enumerate((("Status", "state"), ("Agent", "agent"), ("Server", "server"),
                                            ("Transport", "transport"), ("Jobs in progress", "active"), ("Version", "version"))):
            ttk.Label(frame, text=label + ":").grid(row=row, column=0, sticky="e", **PAD)
            ttk.Label(frame, textvariable=self.vars[key]).grid(row=row, column=1, sticky="w", **PAD)
        buttons = ttk.Frame(frame)
        buttons.grid(row=6, column=0, columnspan=2, pady=10)
        ttk.Button(buttons, text="Check for jobs now", command=self.app.catch_up).pack(side="left", padx=4)
        ttk.Button(buttons, text="Pause / Resume", command=self.app.toggle_pause).pack(side="left", padx=4)
        ttk.Button(buttons, text="Close", command=self.close).pack(side="left", padx=4)

    def refresh(self) -> None:
        s = self.app.status
        self.vars["state"].set(state_label(s))
        self.vars["agent"].set((s or {}).get("agent") or "—")
        self.vars["server"].set((s or {}).get("server") or "—")
        self.vars["transport"].set((s or {}).get("transport") or "—")
        self.vars["active"].set(str((s or {}).get("active_jobs") or 0))
        self.vars["version"].set((s or {}).get("version") or "—")


class HistoryWindow(_Window):
    title = "TBHprint - Print history"
    size = "760x420"

    def build(self) -> None:
        frame = ttk.Frame(self.top, padding=8)
        frame.pack(fill="both", expand=True)
        top = ttk.Frame(frame)
        top.pack(fill="x")
        ttk.Label(top, text="Show:").pack(side="left")
        self.filter_var = tk.StringVar(value="all")
        box = ttk.Combobox(top, textvariable=self.filter_var, state="readonly", width=12,
                           values=("all", "printed", "failed", "skipped", "queued", "printing", "downloading", "cancelled"))
        box.pack(side="left", padx=6)
        box.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        ttk.Button(top, text="Refresh", command=self.refresh).pack(side="left", padx=4)
        ttk.Button(top, text="Reprint selected", command=self.reprint).pack(side="right", padx=4)
        ttk.Button(top, text="Cancel selected", command=self.cancel).pack(side="right", padx=4)
        cols = ("time", "document", "printer", "status", "detail")
        self.tree = ttk.Treeview(frame, columns=cols, show="headings", selectmode="browse")
        for col, width in zip(cols, (110, 220, 150, 80, 200)):
            self.tree.heading(col, text=col.title())
            self.tree.column(col, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True, pady=6)
        self.jobs: dict[str, dict[str, Any]] = {}

    def refresh(self) -> None:
        status = self.filter_var.get()
        rows = self.app.try_call("history", limit=200, status=None if status == "all" else status) or []
        selected = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        self.jobs = {}
        for job in rows:
            iid = str(job["job_id"])
            self.jobs[iid] = job
            self.tree.insert("", "end", iid=iid, values=job_row(job))
        for iid in selected:
            if iid in self.jobs:
                self.tree.selection_set(iid)

    def _selected(self) -> str | None:
        sel = self.tree.selection()
        return sel[0] if sel else None

    def reprint(self) -> None:
        iid = self._selected()
        if iid is None:
            return
        try:
            self.app.call("reprint", id=iid)
            self.app.notify("Reprint submitted")
        except (OSError, control.ControlError) as exc:
            messagebox.showerror("TBHprint", str(exc), parent=self.top)
        self.refresh()

    def cancel(self) -> None:
        iid = self._selected()
        if iid is None:
            return
        try:
            self.app.call("cancel_job", id=iid)
        except (OSError, control.ControlError) as exc:
            messagebox.showerror("TBHprint", str(exc), parent=self.top)
        self.refresh()


class SettingsWindow(_Window):
    title = "TBHprint - Settings"
    size = "700x560"

    def build(self) -> None:
        self.cfg = self.app.try_call("get_config") or (cfgmod.load(self.app.config_path).redacted_dict() if self.app.config_path else {})
        nb = ttk.Notebook(self.top)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self._build_pairing(ttk.Frame(nb, padding=10), nb)
        self._build_printers(ttk.Frame(nb, padding=10), nb)

    # pairing tab ------------------------------------------------------------------

    def _build_pairing(self, frame, nb) -> None:
        nb.add(frame, text="Shop")
        server = self.cfg.get("server") or {}
        paired = bool(server.get("url") and server.get("agent_uuid"))
        self.pair_status = tk.StringVar(value=(f"Paired as \"{server.get('agent_name')}\" with {server.get('url')}" if paired
                                               else "Not paired - enter the code from Settings → Printing → Add agent."))
        ttk.Label(frame, textvariable=self.pair_status, wraplength=620).grid(row=0, column=0, columnspan=2, sticky="w", **PAD)
        self.url_var = tk.StringVar(value=server.get("url") or "https://")
        self.code_var = tk.StringVar()
        self.name_var = tk.StringVar(value=server.get("agent_name") or _hostname())
        for row, (label, var) in enumerate((("Shop URL", self.url_var), ("Pairing code", self.code_var), ("This PC's name", self.name_var)), start=1):
            ttk.Label(frame, text=label + ":").grid(row=row, column=0, sticky="e", **PAD)
            ttk.Entry(frame, textvariable=var, width=48).grid(row=row, column=1, sticky="w", **PAD)
        self.pair_button = ttk.Button(frame, text="Pair" if not paired else "Re-pair", command=self.pair)
        self.pair_button.grid(row=4, column=1, sticky="w", **PAD)
        ttk.Separator(frame).grid(row=5, column=0, columnspan=2, sticky="ew", pady=8)
        transport = self.cfg.get("transport") or {}
        ttk.Label(frame, text="Poll interval when the websocket is down (s):").grid(row=6, column=0, sticky="e", **PAD)
        self.poll_var = tk.IntVar(value=int(transport.get("poll_interval_s") or 60))
        ttk.Spinbox(frame, from_=10, to=600, textvariable=self.poll_var, width=8).grid(row=6, column=1, sticky="w", **PAD)
        ttk.Button(frame, text="Save", command=self.save_transport).grid(row=7, column=1, sticky="w", **PAD)

    def pair(self) -> None:
        url = self.url_var.get().strip().rstrip("/")
        code = self.code_var.get().strip()
        name = self.name_var.get().strip() or _hostname()
        if not url.startswith(("http://", "https://")) or not code:
            messagebox.showwarning("TBHprint", "Enter the shop URL and the pairing code.", parent=self.top)
            return
        self.pair_button.state(["disabled"])
        self.pair_status.set("Pairing…")

        def work():
            try:
                data = apimod.pair(url, code, name)
                cfg = _load_or_empty(self.app.config_path)
                reverb = data.get("reverb") or {}
                cfg.server = cfgmod.Server(
                    url=url, token=str(data["token"]), agent_uuid=str(data["agent_uuid"]),
                    agent_name=str(data.get("name") or name), tenant=str(data.get("tenant") or ""),
                    channel=str(data.get("channel") or ""),
                    reverb=cfgmod.Reverb(key=str(reverb.get("key") or ""), host=str(reverb.get("host") or ""),
                                         port=int(reverb.get("port") or 443), scheme=str(reverb.get("scheme") or "https")),
                )
                cfgmod.save(cfg, self.app.config_path)
                self.app.try_call("reload")
                message = f"Paired as \"{cfg.server.agent_name}\" with {url}"
                ok = True
            except Exception as exc:  # ApiError, RequestException, ConfigError
                message = f"Pairing failed: {exc}"
                ok = False
            self.after(0, lambda: self._paired(ok, message))

        threading.Thread(target=work, daemon=True).start()

    def _paired(self, ok: bool, message: str) -> None:
        self.pair_status.set(message)
        self.pair_button.state(["!disabled"])
        if ok:
            self.code_var.set("")
            self.app.refresh()

    def save_transport(self) -> None:
        try:
            self.app.call("set_config", config={"transport": {"poll_interval_s": int(self.poll_var.get())}})
            self.app.notify("Settings saved")
        except (OSError, control.ControlError) as exc:
            messagebox.showerror("TBHprint", str(exc), parent=self.top)

    # printers tab -------------------------------------------------------------------

    def _build_printers(self, frame, nb) -> None:
        nb.add(frame, text="Printers")
        ttk.Label(frame, text="Which printer each document goes to. Leave blank to refuse that type (the shop sees why).",
                  wraplength=620).grid(row=0, column=0, columnspan=4, sticky="w", **PAD)
        self.printer_names: list[str] = []
        self.rows: list[dict[str, Any]] = []
        header = ("Document", "Printer", "Copies", "Enabled")
        for col, text in enumerate(header):
            ttk.Label(frame, text=text, font=("TkDefaultFont", 9, "bold")).grid(row=1, column=col, sticky="w", **PAD)
        self.row_widgets: list[tuple[dict[str, Any], tk.StringVar, tk.StringVar, tk.BooleanVar]] = []
        for i, row in enumerate(routing_rows(self.cfg), start=2):
            printer_var = tk.StringVar(value=row["printer_name"])
            copies_var = tk.StringVar(value="" if row["copies"] in (None, "") else str(row["copies"]))
            enabled_var = tk.BooleanVar(value=row["enabled"] or not row["printer_name"])
            ttk.Label(frame, text=row["label"]).grid(row=i, column=0, sticky="w", **PAD)
            box = ttk.Combobox(frame, textvariable=printer_var, width=34)
            box.grid(row=i, column=1, sticky="w", **PAD)
            ttk.Entry(frame, textvariable=copies_var, width=6).grid(row=i, column=2, sticky="w", **PAD)
            ttk.Checkbutton(frame, variable=enabled_var).grid(row=i, column=3, sticky="w", **PAD)
            self.row_widgets.append((row, printer_var, copies_var, enabled_var))
            self.rows.append(row)
        self.combos = [w for w in frame.winfo_children() if isinstance(w, ttk.Combobox)]
        last = 2 + len(self.row_widgets)
        ttk.Label(frame, text="Everything else:").grid(row=last, column=0, sticky="w", **PAD)
        default_key = self.cfg.get("default_printer")
        default_name = ((self.cfg.get("printers") or {}).get(default_key or "") or {}).get("name") or ""
        self.default_var = tk.StringVar(value=default_name)
        default_box = ttk.Combobox(frame, textvariable=self.default_var, width=34)
        default_box.grid(row=last, column=1, sticky="w", **PAD)
        self.combos.append(default_box)
        buttons = ttk.Frame(frame)
        buttons.grid(row=last + 1, column=0, columnspan=4, pady=12, sticky="w")
        ttk.Button(buttons, text="Save", command=self.save_routing).pack(side="left", padx=4)
        ttk.Button(buttons, text="Refresh printer list", command=self.load_printers).pack(side="left", padx=4)
        self.test_var = tk.StringVar()
        test_box = ttk.Combobox(buttons, textvariable=self.test_var, width=30)
        test_box.pack(side="left", padx=(16, 4))
        self.combos.append(test_box)
        ttk.Button(buttons, text="Test print", command=self.test_print).pack(side="left", padx=4)
        self.load_printers()

    def load_printers(self) -> None:
        def work():
            names = self.app.try_call("printers")
            if names is None:
                try:
                    from ..backends import get_backend
                    names = get_backend(str(self.cfg.get("backend") or "auto")).list_printers()
                except Exception:
                    names = []
            self.after(0, lambda: self._printers_loaded(list(names or [])))

        threading.Thread(target=work, daemon=True).start()

    def _printers_loaded(self, names: list[str]) -> None:
        self.printer_names = names
        for box in self.combos:
            if box.winfo_exists():
                box["values"] = names

    def save_routing(self) -> None:
        rows = []
        for row, printer_var, copies_var, enabled_var in self.row_widgets:
            copies = copies_var.get().strip()
            if copies and not copies.isdigit():
                messagebox.showwarning("TBHprint", f"Copies for {row['label']} must be a number.", parent=self.top)
                return
            rows.append({"document_type": row["document_type"], "printer_name": printer_var.get(),
                         "copies": int(copies) if copies else None, "enabled": enabled_var.get()})
        update = routing_update(rows, self.cfg, self.default_var.get().strip() or None)
        try:
            self.cfg = self.app.call("set_config", config=update)
            self.app.notify("Printer routing saved")
        except (OSError, control.ControlError) as exc:
            # Agent not running: write the config file directly.
            try:
                cfg = cfgmod.apply_update(_load_or_empty(self.app.config_path), update)
                cfgmod.save(cfg, self.app.config_path)
                self.cfg = cfg.redacted_dict()
                self.app.notify("Printer routing saved (agent not running)")
            except cfgmod.ConfigError as cfg_exc:
                messagebox.showerror("TBHprint", f"{exc}\n{cfg_exc}", parent=self.top)

    def test_print(self) -> None:
        name = self.test_var.get().strip()
        if not name:
            return
        from .model import printer_key
        key = printer_key(name)
        try:
            cfg = self.app.try_call("get_config") or {}
            if key not in (cfg.get("printers") or {}):
                printers = dict(cfg.get("printers") or {})
                printers[key] = {"name": name, "options": []}
                self.app.call("set_config", config={"printers": printers})
            self.app.call("test_print", printer=key)
            self.app.notify("Test page sent")
        except (OSError, control.ControlError) as exc:
            messagebox.showerror("TBHprint", str(exc), parent=self.top)


class LogWindow(_Window):
    title = "TBHprint - Log"
    size = "760x420"

    def build(self) -> None:
        frame = ttk.Frame(self.top, padding=6)
        frame.pack(fill="both", expand=True)
        self.text = tk.Text(frame, wrap="none", font=("Consolas" if _is_windows() else "Monospace", 9))
        self.text.pack(fill="both", expand=True)
        ttk.Button(frame, text="Refresh", command=self.refresh).pack(pady=4)

    def refresh(self) -> None:
        lines = self.app.try_call("get_log_tail", n=300) or ["(agent not running)"]
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", "\n".join(lines))
        self.text.see("end")
        self.text.configure(state="disabled")


def _hostname() -> str:
    import socket
    return socket.gethostname() or "Print agent"


def _is_windows() -> bool:
    import sys
    return sys.platform.startswith("win")


def _load_or_empty(path: str | None) -> cfgmod.Config:
    try:
        return cfgmod.load(path)
    except cfgmod.ConfigError:
        return cfgmod.Config()
