"""The tray icon + menu (pystray) and the applet's main loop (tkinter).

Threading model: tkinter owns the main thread (its mainloop is the
process's event loop, with the root window hidden); pystray runs detached
on its own thread and every menu action - and every request forwarded
through the tray channel (`tbhprint.traychannel`) - is marshalled onto the
Tk thread through a queue drained by `root.after`. A 5-second poll of the
daemon's `status` recolours the icon and refreshes any open window. The Tk
mainloop never runs printing code: on Windows the agent is a separate,
supervised child process (`tbhprint.supervisor`), so a hung UI can never
stop a print job.
"""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import tkinter as tk
from typing import Any, Callable

import pystray

from .. import config as cfgmod
from .. import control
from .. import singleinstance
from .. import supervisor as supervisormod
from .. import traychannel
from . import icons
from .model import state_label, tooltip

log = logging.getLogger("tbhprint.tray")

POLL_MS = 5000


class TrayApplet:
    def __init__(self, client_factory: Callable[[], control.ControlClient] | None = None,
                config_path: str | None = None, supervisor: supervisormod.Supervisor | None = None,
                open_window: str | None = None):
        self.client_factory = client_factory or (lambda: control.ControlClient(timeout=3))
        self.config_path = config_path
        self.supervisor = supervisor          # Windows only - the child agent's supervisor, or None
        self.initial_window = open_window
        self.status: dict[str, Any] | None = None
        self._last_state: str | None = None
        self._offered_settings = False        # opened Settings once already for an unpaired agent
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
            pystray.MenuItem("Check for updates", self._on_tk(self.check_updates)),
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

    def check_updates(self) -> None:
        result = self.try_call("update")
        if result is None:
            self.notify("Could not reach the agent to check for updates")
            return
        version = result.get("version")
        if not version:
            self.notify("TBHprint is up to date")
            return
        notes = f" - {result['notes']}" if result.get("notes") else ""
        self.notify(f"Update {version} installing{notes}")

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
        if self.supervisor is not None:
            self.supervisor.stop()
        self.root.quit()

    # -- requests from the tray channel (a second `tbhprint tray`/`settings`/
    #    `quit` invocation) - these may run on the channel server's own
    #    thread, so marshal through the same action queue as menu clicks. --

    def request_open(self, window: str) -> None:
        self._actions.put(lambda: self.open(window))

    def request_quit(self) -> None:
        self._actions.put(self.quit)

    # -- loop --------------------------------------------------------------------------

    def refresh(self) -> None:
        self.status = self.try_call("status")
        state = "unpaired" if self.status is None else ("paused" if self.status.get("paused") else str(self.status.get("state")))
        if state != self._last_state or self.status is not None:
            badge = int((self.status or {}).get("active_jobs") or 0)
            self.icon.icon = icons.render(state, badge=badge)
            self.icon.title = tooltip(self.status)
            self._last_state = state
        if not self._offered_settings and self.status is not None and self.status.get("state") == "unpaired":
            self._offered_settings = True
            self.open("settings")
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
        if self.initial_window:
            self.root.after(200, lambda: self.open(self.initial_window))
        try:
            self.root.mainloop()
        finally:
            try:
                self.icon.stop()
            except Exception:
                pass


# -- Windows: the tray supervises the agent as a child process ----------------

def _build_supervisor(config_path: str, state_dir: str, *, dry_run: bool, verbose: bool) -> supervisormod.Supervisor:
    log_path = os.path.join(state_dir, "tbhprint.log")

    def spawn() -> subprocess.Popen:
        supervisormod.rotate_log_if_large(log_path)
        os.makedirs(state_dir, exist_ok=True)
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        exe = pythonw if os.path.exists(pythonw) else sys.executable
        argv = [exe, "-m", "tbhprint", "--config", config_path, "run", "--supervised", "--state-dir", state_dir]
        if dry_run:
            argv.append("--dry-run")
        if verbose:
            argv.append("--verbose")
        log_fh = open(log_path, "a", encoding="utf-8")
        try:
            return subprocess.Popen(argv, stdout=log_fh, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        finally:
            log_fh.close()  # the child holds its own duplicated handle

    def check_alive() -> bool:
        client = control.ControlClient(timeout=5)
        try:
            client.call("status")
            return True
        except (OSError, control.ControlError):
            return False
        finally:
            client.close()

    return supervisormod.Supervisor(spawn, check_alive=check_alive)


def main(config_path: str, *, state_dir: str | None = None, dry_run: bool = False,
        verbose: bool = False, open_window: str | None = None) -> int:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    state_dir = state_dir or cfgmod.default_state_dir()

    lock = singleinstance.SingleInstanceLock(singleinstance.TRAY_MUTEX_NAME,
                                             lock_path=os.path.join(state_dir, "tray.lock"))
    try:
        lock.acquire()
    except singleinstance.AlreadyRunning:
        # A tray is already running - forward this request to it instead of
        # starting a second one (and, on Windows, a second supervisor).
        traychannel.send("open", window=open_window)
        return 0

    supervisor_obj = None
    if sys.platform.startswith("win"):
        supervisor_obj = _build_supervisor(config_path, state_dir, dry_run=dry_run, verbose=verbose)

    applet = TrayApplet(config_path=config_path, supervisor=supervisor_obj, open_window=open_window)
    if supervisor_obj is not None:
        supervisor_obj.start()
    channel_server = control.ControlServer(traychannel.TrayDispatcher(applet), address=traychannel.default_address())
    channel_server.start()
    try:
        applet.run()
    finally:
        if supervisor_obj is not None:
            supervisor_obj.stop()
        channel_server.stop()
        lock.release()
    return 0
