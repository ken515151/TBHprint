"""The tray icon + menu (pystray) and the applet's main loop (tkinter).

Threading model: tkinter owns the main thread (its mainloop is the
process's event loop, with the root window hidden); pystray runs detached
on its own thread and every menu action is marshalled onto the Tk thread
through a queue drained by `root.after`. A 5-second poll of the daemon's
`status` recolours the icon and refreshes any open window.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import tkinter as tk
from typing import Any, Callable

import pystray

from .. import control
from . import icons
from .model import DOCUMENT_LABELS, state_label, tooltip

log = logging.getLogger("tbhprint.tray")

POLL_MS = 5000


class TrayApplet:
    def __init__(self, client_factory: Callable[[], control.ControlClient] | None = None,
                 config_path: str | None = None, embedded=None):
        self.client_factory = client_factory or (lambda: control.ControlClient(timeout=3))
        self.config_path = config_path
        self.embedded = embedded            # an EmbeddedDaemon or None
        self.status: dict[str, Any] | None = None
        self._last_state: str | None = None
        self._actions: queue.Queue[Callable[[], None]] = queue.Queue()
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("TBHprint")
        self.windows: dict[str, Any] = {}
        self.icon = pystray.Icon("tbhprint", icons.render("starting"), "TBHprint", menu=self._build_menu())

    # -- control channel ----------------------------------------------------------

    def call(self, cmd: str, **args) -> Any:
        client = self.client_factory()
        try:
            return client.call(cmd, **args)
        finally:
            client.close()

    def try_call(self, cmd: str, **args) -> Any:
        try:
            return self.call(cmd, **args)
        except (OSError, control.ControlError) as exc:
            log.debug("control %s failed: %s", cmd, exc)
            return None

    # -- menu ------------------------------------------------------------------------

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(lambda item: state_label(self.status), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda item: "Resume printing" if (self.status or {}).get("paused") else "Pause printing",
                             self._on_tk(self.toggle_pause),
                             enabled=lambda item: self.status is not None),
            pystray.MenuItem("Check for jobs now", self._on_tk(self.catch_up),
                             enabled=lambda item: bool((self.status or {}).get("paired"))),
            pystray.MenuItem("Test print", pystray.Menu(lambda: self._printer_items())),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Status…", self._on_tk(lambda: self.open("status"))),
            pystray.MenuItem("Print history…", self._on_tk(lambda: self.open("history"))),
            pystray.MenuItem("Settings…", self._on_tk(lambda: self.open("settings"))),
            pystray.MenuItem("Log…", self._on_tk(lambda: self.open("log"))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit TBHprint", self._on_tk(self.quit)),
        )

    def _printer_items(self):
        cfg = self.try_call("get_config") or {}
        printers = (cfg.get("printers") or {})
        if not printers:
            return [pystray.MenuItem("No printers configured", None, enabled=False)]
        return [pystray.MenuItem(p["name"], self._on_tk(lambda key=key: self.test_print(key))) for key, p in printers.items()]

    def _on_tk(self, fn: Callable[[], None]):
        """Wrap a menu action so it runs on the Tk thread."""
        def handler(icon=None, item=None):
            self._actions.put(fn)
        return handler

    # -- actions ---------------------------------------------------------------------

    def toggle_pause(self) -> None:
        if self.status and self.status.get("paused"):
            self.try_call("resume")
        else:
            self.try_call("pause")
        self.refresh()

    def catch_up(self) -> None:
        data = self.try_call("catch_up")
        if data is not None:
            self.notify(f"{data.get('jobs', 0)} open job(s) fetched")

    def test_print(self, printer_key: str) -> None:
        try:
            self.call("test_print", printer=printer_key)
            self.notify("Test page sent")
        except (OSError, control.ControlError) as exc:
            self.notify(f"Test print failed: {exc}")

    def notify(self, message: str) -> None:
        try:
            self.icon.notify(message, "TBHprint")
        except Exception:  # not every backend supports notifications
            log.info("%s", message)

    def open(self, name: str) -> None:
        from . import windows as win
        existing = self.windows.get(name)
        if existing is not None and existing.alive():
            existing.raise_()
            return
        factory = {
            "status": win.StatusWindow, "history": win.HistoryWindow,
            "settings": win.SettingsWindow, "log": win.LogWindow,
        }[name]
        self.windows[name] = factory(self)

    def quit(self) -> None:
        try:
            self.icon.stop()
        except Exception:
            pass
        if self.embedded is not None:
            self.embedded.stop()
        self.root.quit()

    # -- loop --------------------------------------------------------------------------

    def refresh(self) -> None:
        self.status = self.try_call("status")
        state = "unpaired" if self.status is None else ("paused" if self.status.get("paused") else str(self.status.get("state")))
        if state != self._last_state or self.status is not None:
            badge = int((self.status or {}).get("active_jobs") or 0)
            self.icon.icon = icons.render(state, badge=badge)
            self.icon.title = tooltip(self.status)
            self._last_state = state
        for window in list(self.windows.values()):
            if window.alive():
                window.refresh()
        try:
            self.icon.update_menu()
        except Exception:
            pass

    def _tick(self) -> None:
        self.refresh()
        self.root.after(POLL_MS, self._tick)

    def _drain(self) -> None:
        while True:
            try:
                fn = self._actions.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                log.exception("applet action failed")
        self.root.after(100, self._drain)

    def run(self) -> None:
        self.icon.run_detached()
        self.root.after(0, self._tick)
        self.root.after(100, self._drain)
        try:
            self.root.mainloop()
        finally:
            try:
                self.icon.stop()
            except Exception:
                pass


class EmbeddedDaemon:
    """Runs the daemon inside the applet process (Windows: one logon task,
    printers are per-user anyway). Same code path as `tbhprint run`."""

    def __init__(self, config_path: str, state_dir: str | None = None, dry_run: bool = False):
        from ..daemon import build
        self.daemon, self.store, self.pipeline = build(config_path, state_dir=state_dir, dry_run=dry_run)
        self.server = control.ControlServer(control.Dispatcher(self.daemon))

    def start(self) -> None:
        self.server.start()
        self.pipeline.start()
        self.daemon.start_transports()
        threading.Thread(target=self.daemon.maintenance_loop, name="maintenance", daemon=True).start()

    def stop(self) -> None:
        self.server.stop()
        self.daemon.stop()
        self.store.close()


def daemon_reachable() -> bool:
    client = control.ControlClient(timeout=2)
    try:
        client.call("status")
        return True
    except (OSError, control.ControlError):
        return False
    finally:
        client.close()


def main(config_path: str, *, embedded: bool | None = None, state_dir: str | None = None,
         dry_run: bool = False, verbose: bool = False) -> int:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if embedded is None:
        # Windows: embed unless a daemon already answers; Linux: systemd owns it.
        embedded = sys.platform.startswith("win") and not daemon_reachable()
    running = None
    if embedded:
        running = EmbeddedDaemon(config_path, state_dir=state_dir, dry_run=dry_run)
        running.start()
    applet = TrayApplet(config_path=config_path, embedded=running)
    applet.run()
    return 0
