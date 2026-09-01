"""The tray's own control channel - separate from the agent's
(`tbhprint.control`). A second `tbhprint tray` (or `tbhprint settings` /
`tbhprint quit`) invocation uses this to tell the ALREADY-RUNNING tray to
open a window or quit, rather than starting a second tray (and, on
Windows, a second supervisor).

Same wire format as `tbhprint.control` (newline-delimited JSON) and the
same `ControlServer`/`ControlClient` machinery - just a different address
and a different, much smaller, command set.
"""

from __future__ import annotations

import os
import sys

from . import control

WINDOWS_ADDRESS = ("127.0.0.1", 47832)
TRAY_COMMANDS = ("open", "quit")


def default_address() -> str | tuple[str, int]:
    if sys.platform.startswith("win"):
        return WINDOWS_ADDRESS
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return os.path.join(runtime_dir, "tbhprint-tray.sock")


class TrayDispatcher:
    """`{"cmd": "open", "window": "settings"}` and `{"cmd": "quit"}` - the
    only two things the tray channel understands. Both are handed to the
    applet's own thread-safe request_* methods (they marshal onto the Tk
    thread; this dispatcher may run on the channel server's own thread)."""

    def __init__(self, applet):
        self.applet = applet

    def handle(self, request: dict) -> dict:
        try:
            cmd = request.get("cmd")
            if cmd not in TRAY_COMMANDS:
                return {"ok": False, "error": f"unknown tray command {cmd!r}"}
            if cmd == "open":
                window = request.get("window") or "status"
                self.applet.request_open(window)
                return {"ok": True, "data": {"opened": window}}
            self.applet.request_quit()
            return {"ok": True, "data": {"quitting": True}}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


def send(cmd: str, *, timeout: float = 3, **kwargs) -> bool:
    """Best-effort: tell a running tray to do something. Returns False
    (never raises) when no tray is listening - the caller's cue to start
    one itself instead."""
    client = control.ControlClient(address=default_address(), timeout=timeout)
    try:
        client.call(cmd, **kwargs)
        return True
    except (OSError, control.ControlError):
        return False
    finally:
        client.close()
